from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from hashlib import sha256
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from merlin_contracts import (
    GOAT_TOOLBOX_CATALOG_DIGEST,
    HYDRA_SELECT_CATALOG_DIGEST,
    TechniqueAttempt,
    TechniqueAttemptControlKind,
    TechniqueAttemptState,
    trusted_goat_toolbox,
    trusted_hydra_select_mechanisms,
)
from psycopg.types.json import Jsonb

from .technique_attempt_store import TechniqueAttemptRepository


class StaleLeaseError(RuntimeError):
    """An attempt no longer owns the node generation it tried to mutate."""


_ATTEMPT_FAILURE_KINDS = frozenset(
    {
        "adapter_timeout",
        "authorization",
        "budget",
        "cleanup_unresolved",
        "configuration",
        "missing_target_effect",
        "pointer_repair",
        "provider_transient",
        "provider_truncated",
        "result_schema_invalid",
        "scope",
        "state_integrity",
        "target_adapter_unhealthy",
        "transport_transient",
        "unknown",
        "worker_interrupted",
    }
)

_MAX_SCHEDULE_JSONB_BYTES = 64 * 1024 * 1024


class ScheduleArtifactTooLarge(ValueError):
    """A JSONB scheduler artifact exceeded the database safety backstop."""

    def __init__(self, *, artifact: str, actual_bytes: int, limit_bytes: int) -> None:
        self.artifact = artifact
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"schedule {artifact} serializes to {actual_bytes:,} bytes, exceeding Merlin's "
            f"{limit_bytes:,}-byte safety limit; reduce the execution batch and reference "
            "shared immutable context instead of copying it per worker"
        )


def _bounded_schedule_jsonb(value: object, *, artifact: str) -> tuple[Jsonb, str]:
    """Build a bounded JSONB parameter and digest from one canonical serialization."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    size = len(encoded)
    if size > _MAX_SCHEDULE_JSONB_BYTES:
        raise ScheduleArtifactTooLarge(
            artifact=artifact,
            actual_bytes=size,
            limit_bytes=_MAX_SCHEDULE_JSONB_BYTES,
        )
    return Jsonb(value), sha256(encoded).hexdigest()


def _serialized(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: DurableSchedulerStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def _transactional(method: Any) -> Any:
    """Keep a boundary check and its mutation in one database transaction."""

    @wraps(method)
    def wrapped(self: DurableSchedulerStore, *args: Any, **kwargs: Any) -> Any:
        with self.connection.transaction():
            return method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class AttemptLease:
    id: str
    schedule_id: str
    node_id: str
    generation: int
    owner: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    id: str
    schedule_id: str
    sender: str
    recipient: str
    schema_id: str
    payload: dict[str, object]
    payload_digest: str


def _qualified_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _goat_checkpoint_digest(value: object) -> str:
    """Match the canonical digest written and validated by ``GoatState``."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _hydra_checkpoint_digest(value: object) -> str:
    """Match the canonical digest written and validated by ``HydraPlan``."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _adaptive_observation_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _as_qualified_digest(value: object) -> str:
    text = str(value)
    return text if text.startswith("sha256:") else f"sha256:{text}"


def _exchange_measurements(exchanges: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Quantity receipt for one stage's acknowledged target exchanges.

    Token quantities are local character-based estimates (four characters per
    token) because target APIs do not report provider usage; they are labelled
    as estimates and never cost figures. Wall-clock latency is the sum of the
    per-exchange ``latency_ms`` measured around the actual target call; it is
    ``None`` unless every exchange carried a non-negative measurement.
    """
    input_chars = 0
    output_chars = 0
    latency_ms = 0
    latency_complete = True
    for item in exchanges:
        input_chars += len(json.dumps(item.get("request"), ensure_ascii=False))
        output_chars += len(json.dumps(item.get("response"), ensure_ascii=False))
        value = item.get("latency_ms")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            latency_complete = False
        else:
            latency_ms += value
    return {
        "request_count": len(exchanges),
        "input_tokens_estimated": -(-input_chars // 4),
        "output_tokens_estimated": -(-output_chars // 4),
        "latency_ms": latency_ms if latency_complete else None,
        "latency_complete": latency_complete,
    }


def _amplification_ratio(numerator: object, denominator: object) -> float | None:
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, (int, float))
        or not isinstance(denominator, (int, float))
        or numerator < 0
        or denominator <= 0
    ):
        return None
    return round(numerator / denominator, 6)


def _comparison_amplification(
    stage_receipts: Sequence[Mapping[str, object]],
) -> dict[str, object] | None:
    """Derive attack-over-control amplification ratios from sealed stage measurements."""
    by_stage = {
        str(item.get("stage")): item.get("measurements")
        for item in stage_receipts
        if isinstance(item, Mapping)
    }
    control = by_stage.get("control")
    direct = by_stage.get("direct")
    if not isinstance(control, Mapping) or not isinstance(direct, Mapping):
        return None
    return {
        "policy": "attack-over-control",
        "input_token_ratio": _amplification_ratio(
            direct.get("input_tokens_estimated"), control.get("input_tokens_estimated")
        ),
        "output_token_ratio": _amplification_ratio(
            direct.get("output_tokens_estimated"), control.get("output_tokens_estimated")
        ),
        "latency_ratio": _amplification_ratio(direct.get("latency_ms"), control.get("latency_ms")),
        "request_count_ratio": _amplification_ratio(
            direct.get("request_count"), control.get("request_count")
        ),
    }


def _observation_only_strategy(
    value: Mapping[str, object], *, verdict_key: str
) -> dict[str, object]:
    """Expose legacy evaluator output as evidence, never scheduler authority."""
    projected = dict(value)
    recorded = projected.get(verdict_key)
    projected[f"recorded_{verdict_key}"] = recorded
    projected[verdict_key] = "ambiguous"
    projected["evaluation_uncertain"] = True
    projected["permanent"] = False
    return projected


def _valid_oracle_observation(value: Mapping[str, object]) -> bool:
    if value.get("schema") == "merlin.api-oracle-observation.v2":
        return value.get("purpose") == "objective" and _valid_adaptive_observation(value)
    if value.get("schema") != "merlin.api-oracle-observation.v1":
        return False
    observation_digest = value.get("observation_digest")
    if not isinstance(observation_digest, str) or not _is_qualified_digest(observation_digest):
        return False
    unsigned = {key: item for key, item in value.items() if key != "observation_digest"}
    binding = {
        key: value.get(key)
        for key in (
            "adapter_digest",
            "step_id",
            "oracle_digest",
            "pointer",
            "match_values",
            "modality",
            "turn",
            "language",
        )
    }
    return (
        observation_digest == _qualified_digest(unsigned)
        and value.get("binding_digest") == _qualified_digest(binding)
        and (value.get("matched") is None or isinstance(value.get("matched"), bool))
        and all(
            isinstance(value.get(key), str) and _is_qualified_digest(str(value[key]))
            for key in ("adapter_digest", "oracle_digest", "binding_digest")
        )
    )


def _valid_adaptive_observation(value: Mapping[str, object]) -> bool:
    if value.get("schema") != "merlin.api-oracle-observation.v2":
        return False
    observation_digest = value.get("observation_digest")
    if not isinstance(observation_digest, str) or not _is_qualified_digest(observation_digest):
        return False
    unsigned = {key: item for key, item in value.items() if key != "observation_digest"}
    binding_keys = (
        "adapter_digest",
        "step_id",
        "oracle_digest",
        "pointer",
        "match_values",
        "modality",
        "turn",
        "language",
        "purpose",
    )
    if "compiled_adapter_digest" in value:
        binding_keys = (*binding_keys, "compiled_adapter_digest")
    return bool(
        observation_digest == _adaptive_observation_digest(unsigned)
        and isinstance(value.get("matched"), bool)
        and all(key in value for key in binding_keys)
        and all(
            isinstance(value.get(key), str) and _is_qualified_digest(str(value[key]))
            for key in ("adapter_digest", "oracle_digest", "compiled_adapter_digest")
            if key in value
        )
        and value.get("binding_digest")
        == _adaptive_observation_digest({key: value[key] for key in binding_keys})
    )


def _is_qualified_digest(value: str) -> bool:
    if len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


class DurableSchedulerStore:
    """PostgreSQL authority boundary for durable scheduler transitions."""

    _zero_digest = "0" * 64

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        connection_lock = getattr(connection, "_merlin_operation_lock", None)
        if connection_lock is None:
            connection_lock = RLock()
            connection._merlin_operation_lock = connection_lock
        self._lock = connection_lock

    @_serialized
    def create_schedule(
        self,
        session_id: str,
        graph: dict[str, object],
        checkpoint: dict[str, object],
        node_ids: Sequence[str],
        *,
        ready_nodes: Sequence[str] = (),
        schedule_id: str | None = None,
        initial_status: str = "running",
        parent_schedule_id: str | None = None,
        parent_attempt_id: str | None = None,
    ) -> str:
        identifier = schedule_id or str(uuid4())
        if initial_status not in {"running", "paused"}:
            raise ValueError("initial schedule status must be running or paused")
        graph_jsonb, graph_digest = _bounded_schedule_jsonb(graph, artifact="graph")
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        ready = set(ready_nodes)
        if (parent_schedule_id is None) != (parent_attempt_id is None):
            raise ValueError("parent schedule and attempt must be provided together")
        if not node_ids or not ready.issubset(node_ids):
            raise ValueError("schedule nodes must be non-empty and contain every ready node")
        with self.connection.transaction():
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_schedules
                       (id, session_id, graph, graph_digest, checkpoint, checkpoint_digest,
                        last_event_digest, status, parent_schedule_id, parent_attempt_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING RETURNING id""",
                (
                    identifier,
                    session_id,
                    graph_jsonb,
                    graph_digest,
                    checkpoint_jsonb,
                    checkpoint_digest,
                    self._zero_digest,
                    initial_status,
                    parent_schedule_id,
                    parent_attempt_id,
                ),
            ).fetchone()
            if inserted is None:
                existing = self.connection.execute(
                    """SELECT session_id, graph_digest, checkpoint_digest,
                              parent_schedule_id, parent_attempt_id
                       FROM merlin_storage.scheduler_schedules WHERE id = %s""",
                    (identifier,),
                ).fetchone()
                if existing is None or (
                    str(existing["session_id"]) != str(session_id)
                    or existing["graph_digest"] != graph_digest
                    or existing["checkpoint_digest"] != checkpoint_digest
                    or (
                        str(existing["parent_schedule_id"])
                        if existing["parent_schedule_id"] is not None
                        else None
                    )
                    != parent_schedule_id
                    or (
                        str(existing["parent_attempt_id"])
                        if existing["parent_attempt_id"] is not None
                        else None
                    )
                    != parent_attempt_id
                ):
                    raise ValueError("deterministic schedule id conflicts with different content")
                return identifier
            for node_id in node_ids:
                self.connection.execute(
                    """INSERT INTO merlin_storage.scheduler_nodes
                           (schedule_id, node_id, state) VALUES (%s, %s, %s)""",
                    (identifier, node_id, "ready" if node_id in ready else "pending"),
                )
            self._append_event(
                identifier,
                "schedule_created",
                {"graph_digest": graph_digest, "status": initial_status},
            )
        return identifier

    @_serialized
    def load_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT id, session_id, graph, graph_digest, checkpoint, checkpoint_digest,
                      event_count, last_event_digest, status, parent_schedule_id,
                      parent_attempt_id, created_at, updated_at
               FROM merlin_storage.scheduler_schedules WHERE id = %s""",
            (schedule_id,),
        ).fetchone()
        if row is None:
            return None
        return _schedule_row(row)

    @_serialized
    def list_schedules(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id, session_id, graph, graph_digest, checkpoint, checkpoint_digest,
                      event_count, last_event_digest, status, parent_schedule_id,
                      parent_attempt_id, created_at, updated_at
               FROM merlin_storage.scheduler_schedules WHERE session_id = %s
               ORDER BY updated_at DESC, id DESC LIMIT %s""",
            (session_id, limit),
        ).fetchall()
        return [_schedule_row(row) for row in rows]

    @_serialized
    def get_assignment_transcript_rows(
        self, session_id: str, assignment_id: str
    ) -> list[dict[str, Any]]:
        """Read retained attempt checkpoints for one worker within its owning session."""
        rows = self.connection.execute(
            """SELECT attempt.generation, attempt.state, attempt.error,
                      result.payload
               FROM merlin_storage.scheduler_schedules AS schedule
               JOIN merlin_storage.scheduler_attempts AS attempt
                 ON attempt.schedule_id = schedule.id
               LEFT JOIN merlin_storage.scheduler_results AS result
                 ON result.attempt_id = attempt.id
               WHERE schedule.session_id = %s AND attempt.node_id = %s
               ORDER BY attempt.started_at, attempt.generation, attempt.id""",
            (session_id, f"worker:{assignment_id}"),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def list_swarm_snapshot_rows(
        self,
        session_id: str,
        *,
        campaign_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one session-scoped swarm batch without restoring live state.

        The persisted schedule graph is the identity source for new rows.  Old
        graphs remain visible, but are marked as legacy by the service
        projection instead of acquiring fabricated durable identities here.
        """
        rows = self.connection.execute(
            """WITH scoped_schedules AS (
                   SELECT schedule.id, schedule.session_id, schedule.graph,
                          schedule.status, schedule.updated_at
                   FROM merlin_storage.scheduler_schedules AS schedule
                   WHERE schedule.session_id = %s
                   ORDER BY schedule.updated_at DESC, schedule.id DESC
               )
               SELECT schedule.id AS schedule_id,
                      schedule.session_id,
                      schedule.status AS schedule_status,
                      campaign.id AS campaign_id,
                      dynamic_specialist.id AS durable_specialist_id,
                      COALESCE(
                          dynamic_specialist.record ->> 'label',
                          dynamic_specialist.record #>> '{profile,summary}'
                      ) AS durable_specialist_display_name,
                      assignment.value AS assignment,
                      candidate.value AS candidate,
                      node.node_id,
                      node.state AS node_state,
                      current_attempt.id AS current_attempt_id,
                      current_attempt.state AS current_attempt_state,
                      current_attempt.owner AS current_attempt_owner,
                      current_attempt.generation AS current_attempt_generation,
                      current_attempt.error AS current_attempt_error,
                      recent_activity.events AS recent_activity,
                      recent_output.messages AS recent_output,
                      current_result.payload ->> 'content' AS current_result_content,
                      technique.state AS evaluation_state,
                      technique.terminal_reason AS evaluation_reason,
                      technique.strategy_state AS evaluation_detail,
                      successful_result.attempt_id AS successful_result_id,
                      successful_result.payload AS successful_result_payload
               FROM scoped_schedules AS schedule
               CROSS JOIN LATERAL jsonb_array_elements(
                   COALESCE(schedule.graph -> 'assignments', '[]'::jsonb)
               ) AS assignment(value)
               CROSS JOIN LATERAL jsonb_array_elements(
                   CASE
                     WHEN jsonb_typeof(
                         assignment.value #> '{swarm_projection,candidates}'
                     ) = 'array'
                       AND jsonb_array_length(
                           assignment.value #> '{swarm_projection,candidates}'
                       ) > 0
                     THEN assignment.value #> '{swarm_projection,candidates}'
                     ELSE '[{}]'::jsonb
                   END
               ) AS candidate(value)
               JOIN merlin_storage.scheduler_nodes AS node
                 ON node.schedule_id = schedule.id
                AND node.node_id = 'worker:' || (assignment.value ->> 'id')
               LEFT JOIN LATERAL (
                   SELECT scoped_campaign.id
                   FROM merlin_storage.assessment_campaigns AS scoped_campaign
                   LEFT JOIN merlin_storage.campaign_waves AS wave
                     ON wave.campaign_id = scoped_campaign.id
                   WHERE scoped_campaign.session_id = schedule.session_id
                     AND (
                         scoped_campaign.schedule_id = schedule.id
                         OR wave.schedule_id = schedule.id
                     )
                   ORDER BY scoped_campaign.updated_at DESC, scoped_campaign.id DESC
                   LIMIT 1
               ) AS campaign ON TRUE
               LEFT JOIN merlin_storage.campaign_dynamic_specialists AS dynamic_specialist
                 ON dynamic_specialist.campaign_id = campaign.id
                AND dynamic_specialist.id = assignment.value ->> 'profile_id'
               LEFT JOIN LATERAL (
                   SELECT attempt.id, attempt.state, attempt.owner,
                          attempt.generation, attempt.error
                   FROM merlin_storage.scheduler_attempts AS attempt
                   WHERE attempt.schedule_id = schedule.id
                     AND attempt.node_id = node.node_id
                   ORDER BY attempt.generation DESC, attempt.started_at DESC, attempt.id DESC
                   LIMIT 1
               ) AS current_attempt ON TRUE
               LEFT JOIN merlin_storage.scheduler_results AS current_result
                 ON current_result.attempt_id = current_attempt.id
               LEFT JOIN LATERAL (
                   SELECT jsonb_agg(output.message ORDER BY output.position) AS messages
                   FROM (
                       SELECT message.value AS message, message.ordinality AS position
                       FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(current_result.payload
                                #> '{worker_checkpoint,messages}') = 'array'
                           THEN current_result.payload #> '{worker_checkpoint,messages}'
                           ELSE '[]'::jsonb END
                       ) WITH ORDINALITY AS message(value, ordinality)
                       WHERE message.value ->> 'role' IN ('assistant', 'tool')
                       ORDER BY message.ordinality DESC
                       LIMIT 4
                   ) AS output
               ) AS recent_output ON TRUE
               LEFT JOIN LATERAL (
                   SELECT jsonb_agg(activity.event ORDER BY activity.sequence) AS events
                   FROM (
                       SELECT event.sequence,
                              jsonb_build_object('type', event.type, 'payload', event.payload,
                                                 'created_at', event.created_at) AS event
                       FROM merlin_storage.scheduler_events AS event
                       WHERE event.schedule_id = schedule.id
                         AND (event.payload ->> 'node_id' = node.node_id
                              OR event.payload ->> 'attempt_id' = current_attempt.id::text)
                       ORDER BY event.sequence DESC
                       LIMIT 3
                   ) AS activity
               ) AS recent_activity ON TRUE
               LEFT JOIN LATERAL (
                   SELECT result.attempt_id, result.payload
                   FROM merlin_storage.scheduler_results AS result
                   JOIN merlin_storage.scheduler_attempts AS attempt
                     ON attempt.id = result.attempt_id
                    AND attempt.state = 'completed'
                   WHERE result.schedule_id = schedule.id
                     AND result.node_id = node.node_id
                   ORDER BY result.created_at DESC, result.attempt_id DESC
                   LIMIT 1
               ) AS successful_result ON TRUE
               LEFT JOIN LATERAL (
                   SELECT attempt.state, attempt.terminal_reason, attempt.strategy_state
                   FROM merlin_storage.campaign_technique_attempts AS attempt
                   WHERE attempt.campaign_id = campaign.id
                     AND attempt.task_id = assignment.value ->> 'id'
                     AND attempt.technique_id = candidate.value ->> 'technique_id'
                     AND attempt.strategy_id = candidate.value ->> 'controller_id'
                     AND attempt.language = candidate.value ->> 'requested_language'
                     AND coalesce(attempt.transform_id, 'baseline') =
                         candidate.value ->> 'payload_family'
                   ORDER BY attempt.updated_at DESC, attempt.id DESC
                   LIMIT 1
               ) AS technique ON TRUE
               WHERE (%s::text IS NULL OR campaign.id = %s)
               ORDER BY schedule.updated_at DESC, schedule.id DESC,
                        assignment.value ->> 'id',
                        candidate.value ->> 'technique_id'""",
            (session_id, campaign_id, campaign_id),
        ).fetchall()
        return [
            {
                **row,
                "schedule_id": str(row["schedule_id"]),
                "session_id": str(row["session_id"]),
                "campaign_id": (
                    str(row["campaign_id"]) if row["campaign_id"] is not None else None
                ),
                "current_attempt_id": (
                    str(row["current_attempt_id"])
                    if row["current_attempt_id"] is not None
                    else None
                ),
                "successful_result_id": (
                    str(row["successful_result_id"])
                    if row["successful_result_id"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    @_serialized
    def list_active_schedules(self, *, limit: int | None = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id, session_id, graph, graph_digest, checkpoint, checkpoint_digest,
                      event_count, last_event_digest, status, parent_schedule_id,
                      parent_attempt_id, created_at, updated_at
               FROM merlin_storage.scheduler_schedules
               WHERE status IN ('running', 'paused')
                  OR (
                       status = 'cancelled'
                       AND EXISTS (
                           SELECT 1 FROM merlin_storage.scheduler_nodes AS node
                           WHERE node.schedule_id = scheduler_schedules.id
                             AND node.node_id LIKE 'cleanup:%%'
                             AND node.state IN ('ready', 'leased')
                       )
                  )
               ORDER BY updated_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
        return [_schedule_row(row) for row in rows]

    @_serialized
    def find_active_task(self, task_id: str) -> list[dict[str, Any]]:
        node_id = task_id if task_id.startswith("worker:") else f"worker:{task_id}"
        rows = self.connection.execute(
            """SELECT schedule.id, schedule.session_id, schedule.graph,
                      schedule.graph_digest, schedule.checkpoint,
                      schedule.checkpoint_digest, schedule.event_count,
                      schedule.last_event_digest, schedule.status,
                      schedule.created_at, schedule.updated_at, node.node_id
               FROM merlin_storage.scheduler_schedules AS schedule
               JOIN merlin_storage.scheduler_nodes AS node
                 ON node.schedule_id = schedule.id
               WHERE schedule.status = 'running' AND node.node_id = %s
                 AND node.state NOT IN ('completed', 'failed', 'cancelled', 'quarantined')
               ORDER BY schedule.updated_at DESC, schedule.id DESC""",
            (node_id,),
        ).fetchall()
        return [{**row, "id": str(row["id"]), "session_id": str(row["session_id"])} for row in rows]

    @_serialized
    def list_nodes(self, schedule_id: str) -> list[dict[str, Any]]:
        return self.connection.execute(
            """SELECT node_id, state, lease_generation, attempts_started,
                      failed_attempts, result_digest
               FROM merlin_storage.scheduler_nodes
               WHERE schedule_id = %s ORDER BY node_id""",
            (schedule_id,),
        ).fetchall()

    @_serialized
    def list_attempts(self, schedule_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id, schedule_id, node_id, generation, owner, state,
                      lease_expires_at, heartbeat_at, started_at, completed_at, error,
                      replaced_attempt_id, target_session_id, failure_kind
               FROM merlin_storage.scheduler_attempts
               WHERE schedule_id = %s ORDER BY started_at, id""",
            (schedule_id,),
        ).fetchall()
        return [
            {
                **row,
                "id": str(row["id"]),
                "schedule_id": str(row["schedule_id"]),
                "replaced_attempt_id": str(row["replaced_attempt_id"])
                if row["replaced_attempt_id"] is not None
                else None,
            }
            for row in rows
        ]

    @_serialized
    def list_results(self, schedule_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT attempt_id, schedule_id, node_id, generation, schema_id,
                      payload, payload_digest, created_at
               FROM merlin_storage.scheduler_results
               WHERE schedule_id = %s ORDER BY created_at, attempt_id""",
            (schedule_id,),
        ).fetchall()
        return [
            {
                **row,
                "attempt_id": str(row["attempt_id"]),
                "schedule_id": str(row["schedule_id"]),
            }
            for row in rows
        ]

    @_serialized
    def replacement_attempt_lineage(self, attempt_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """WITH RECURSIVE lineage AS (
                   SELECT id, replaced_attempt_id, 0 AS depth
                   FROM merlin_storage.scheduler_attempts WHERE id = %s
                   UNION ALL
                   SELECT prior.id, prior.replaced_attempt_id, lineage.depth + 1
                   FROM merlin_storage.scheduler_attempts prior
                   JOIN lineage ON prior.id = lineage.replaced_attempt_id
               )
               SELECT id FROM lineage ORDER BY depth DESC""",
            (attempt_id,),
        ).fetchall()
        values = tuple(str(row["id"]) for row in rows)
        if not values or values[-1] != attempt_id:
            raise KeyError(attempt_id)
        return values

    @_serialized
    def baseline_replay_binding(
        self, schedule_id: str, predecessor_assignment_id: str
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            """SELECT attempt.prompt_digest, attempt.target_effect_id, effect.tool_name,
                      effect.call_id, effect.result_metadata, result.payload,
                      attempt.target_session_id,
                      attempt.target_session_policy, attempt.witness_effect_ids
               FROM merlin_storage.baseline_technique_attempts attempt
               JOIN merlin_storage.tool_effects effect
                 ON effect.idempotency_key = attempt.prompt_effect_id
               JOIN merlin_storage.scheduler_results result
                 ON result.attempt_id = attempt.scheduler_attempt_id
               WHERE attempt.schedule_id = %s AND attempt.stage = 'direct'
                 AND attempt.outcome = 'completed'
                 AND result.node_id = %s""",
            (schedule_id, f"worker:{predecessor_assignment_id}"),
        ).fetchone()
        if row is None:
            return None
        sequence_rows = self.connection.execute(
            """SELECT tool_name, arguments_digest, result_metadata
               FROM merlin_storage.tool_effects
               WHERE idempotency_key = ANY(%s)
               ORDER BY array_position(%s::text[], idempotency_key)""",
            (row["witness_effect_ids"], row["witness_effect_ids"]),
        ).fetchall()
        target_arguments_digest = (row["result_metadata"] or {}).get("target_arguments_digest")
        return {
            "tool_name": row["tool_name"],
            "arguments_digest": target_arguments_digest or row["prompt_digest"],
            "call_id": row["call_id"],
            "target_session_id": row["target_session_id"],
            "target_session_policy": row["target_session_policy"],
            "payload": row["payload"],
            "target_sequence": tuple(
                (
                    str(item["tool_name"]),
                    str(
                        (item["result_metadata"] or {}).get("target_arguments_digest")
                        or item["arguments_digest"]
                    ),
                )
                for item in sequence_rows
            ),
        }

    @_serialized
    def baseline_predecessor_completed(
        self, schedule_id: str, predecessor_assignment_id: str, expected_stage: str
    ) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM merlin_storage.baseline_technique_attempts stage
               JOIN merlin_storage.scheduler_results result
                 ON result.attempt_id = stage.scheduler_attempt_id
               WHERE stage.schedule_id = %s AND stage.stage = %s
                 AND stage.outcome = 'completed' AND result.node_id = %s""",
            (schedule_id, expected_stage, f"worker:{predecessor_assignment_id}"),
        ).fetchone()
        return row is not None

    @_serialized
    def baseline_gate(self, schedule_id: str) -> dict[str, object]:
        self._reconcile_baseline_comparisons(schedule_id)
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        assignments_by_agenda: dict[str, list[Mapping[str, object]]] = {}
        if schedule is not None:
            for item in schedule["graph"].get("assignments", ()):
                if isinstance(item, Mapping) and item.get("agenda_id"):
                    assignments_by_agenda.setdefault(str(item["agenda_id"]), []).append(item)
        rows = self.connection.execute(
            """SELECT id::text, agenda_id, technique_id, pair_id, stage, scheduler_attempt_id,
                      predecessor_id, prompt_digest, oracle_digest, target_session_id,
                      target_session_policy,
                      prompt_effect_id, target_effect_id, witness_effect_ids,
                      outcome, reason, result_digest
               FROM merlin_storage.baseline_technique_attempts
               WHERE schedule_id = %s ORDER BY agenda_id,
                 CASE stage WHEN 'control' THEN 1 WHEN 'direct' THEN 2 ELSE 3 END""",
            (schedule_id,),
        ).fetchall()
        by_agenda: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            value = dict(row)
            for key in ("scheduler_attempt_id", "predecessor_id"):
                if value.get(key) is not None:
                    value[key] = str(value[key])
            by_agenda.setdefault(str(row["agenda_id"]), []).append(value)
        execution_completed = {
            agenda
            for agenda, stages in by_agenda.items()
            if [stage["stage"] for stage in stages]
            == sorted(
                (
                    str(item["execution_stage"])
                    for item in assignments_by_agenda.get(agenda, ())
                    if item.get("execution_stage") in {"control", "direct", "replay"}
                ),
                key={"control": 0, "direct": 1, "replay": 2}.__getitem__,
            )
            and all(stage["outcome"] == "completed" for stage in stages)
        }
        comparison_rows = self.connection.execute(
            """SELECT agenda_id, verdict, reason, pair_id, policy, policy_digest,
                      oracle_binding_digest, adapter_digest, stage_receipts,
                      transform_artifacts, receipt_digest
               FROM merlin_storage.baseline_comparison_receipts
               WHERE schedule_id = %s ORDER BY agenda_id""",
            (schedule_id,),
        ).fetchall()
        comparisons = {}
        for row in comparison_rows:
            value = _observation_only_strategy(dict(row), verdict_key="verdict")
            value["amplification"] = _comparison_amplification(
                row["stage_receipts"] if isinstance(row["stage_receipts"], list) else ()
            )
            comparisons[str(row["agenda_id"])] = value
        for agenda_id in assignments_by_agenda:
            # Legacy unsupported metadata is evaluation evidence, not authority
            # to invent a result before any target execution.
            if agenda_id in comparisons or agenda_id in by_agenda:
                continue
        for agenda_id, value in comparisons.items():
            # Terminal receipts sealed without executed stages (capability-blocked
            # pairs) carry no stage rows; expose the direct assignment's technique
            # so consumers can attribute the outcome to the owning task.
            if agenda_id in by_agenda:
                continue
            direct_assignment = next(
                (
                    item
                    for item in assignments_by_agenda.get(agenda_id, ())
                    if item.get("execution_stage") in {"direct", "replay"}
                    and item.get("technique_ids")
                ),
                None,
            )
            if direct_assignment is not None:
                value["technique_id"] = str(direct_assignment["technique_ids"][0])
        ambiguous = tuple(sorted(comparisons))
        execution_complete = tuple(sorted(execution_completed))
        return {
            "complete": execution_complete,
            "execution_complete": execution_complete,
            "comparison_qualified": (),
            "positive": (),
            "negative": (),
            "ambiguous": ambiguous,
            "blocked": (),
            "comparisons": comparisons,
            "agendas": {key: tuple(value) for key, value in sorted(by_agenda.items())},
        }

    @_serialized
    def comparison_receipt(self, receipt_digest: str) -> dict[str, Any] | None:
        """Load a sealed comparison receipt by digest; fail closed on seal drift."""
        row = self.connection.execute(
            """SELECT schedule_id::text AS schedule_id, agenda_id, pair_id, policy,
                      policy_digest, oracle_binding_digest, adapter_digest, stage_receipts,
                      transform_artifacts, verdict, reason, receipt_digest
               FROM merlin_storage.baseline_comparison_receipts
               WHERE receipt_digest = %s""",
            (receipt_digest,),
        ).fetchone()
        if row is None:
            return None
        unsigned = {
            key: row[key]
            for key in (
                "pair_id",
                "policy",
                "policy_digest",
                "oracle_binding_digest",
                "adapter_digest",
                "stage_receipts",
                "transform_artifacts",
                "verdict",
                "reason",
            )
        }
        if row["receipt_digest"] != _qualified_digest(unsigned):
            return None
        result = _observation_only_strategy(dict(row), verdict_key="verdict")
        result["amplification"] = _comparison_amplification(
            row["stage_receipts"] if isinstance(row["stage_receipts"], list) else ()
        )
        return result

    @_serialized
    def crescendo_gate(self, schedule_id: str) -> dict[str, object]:
        """Rebuild immutable adaptive receipts from acknowledged tool effects."""
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignments = tuple(
            item
            for item in schedule["graph"].get("assignments", ())
            if isinstance(item, Mapping) and item.get("strategy_id") == "crescendo"
        )
        drifted: set[str] = set()
        for assignment in assignments:
            try:
                self._reconcile_crescendo_assignment(schedule_id, assignment)
            except ValueError:
                drifted.add(str(assignment.get("id", "")))
        rows = self.connection.execute(
            """SELECT assignment_id, semantic_technique_id, adapter_digest,
                      contract_digest, target_session_id,
                      terminal_reason, verdict, step_receipt_digests, receipt_digest
               FROM merlin_storage.crescendo_terminal_receipts
               WHERE schedule_id = %s ORDER BY assignment_id""",
            (schedule_id,),
        ).fetchall()
        terminal_strategies = {
            str(row["assignment_id"]): _observation_only_strategy(dict(row), verdict_key="verdict")
            for row in rows
        }
        assignment_by_id = {str(item["id"]): item for item in assignments}
        node_states = {
            str(row["node_id"])[len("worker:") :]: str(row["state"])
            for row in self.connection.execute(
                """SELECT node_id, state FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id LIKE 'worker:%%'""",
                (schedule_id,),
            ).fetchall()
        }
        strategies = {
            assignment_id: terminal_strategies.get(
                assignment_id,
                {
                    "assignment_id": assignment_id,
                    "semantic_technique_id": assignment.get("semantic_technique_id"),
                    "terminal_reason": None,
                    "verdict": "ambiguous",
                    "recorded_verdict": None,
                    "evaluation_uncertain": True,
                    "permanent": False,
                },
            )
            for assignment_id, assignment in assignment_by_id.items()
        }
        for assignment_id, strategy in strategies.items():
            assignment = assignment_by_id.get(assignment_id, {})
            strategy["technique_ids"] = tuple(assignment.get("technique_ids", ()))
            strategy["strategy_contract"] = tuple(assignment.get("strategy_contract", ()))
            strategy["execution_complete"] = node_states.get(
                assignment_id
            ) == "completed" and strategy.get("terminal_reason") not in {
                "ambiguous",
                "cancelled",
                "blocked",
            }
            strategy["steps"] = tuple(
                dict(row)
                for row in self.connection.execute(
                    """SELECT step_index, stage, scheduler_attempt_id, effect_id, call_id,
                              target_session_id, arguments_digest, prompt_digest, exchange_id,
                              exchange_digest, observation_digests, decision, receipt_digest,
                              contract_digest, adapter_digest, semantic_technique_id
                       FROM merlin_storage.crescendo_step_receipts
                       WHERE schedule_id = %s AND assignment_id = %s ORDER BY step_index""",
                    (schedule_id, assignment_id),
                ).fetchall()
            )
        for assignment_id in drifted:
            if assignment_id in strategies:
                strategies[assignment_id] = {
                    **strategies[assignment_id],
                    "verdict": "ambiguous",
                    "terminal_reason": "ambiguous",
                }
        execution_complete = tuple(
            sorted(
                key for key, value in strategies.items() if value.get("execution_complete") is True
            )
        )
        return {
            "complete": execution_complete,
            "execution_complete": execution_complete,
            "positive": (),
            "negative": (),
            "ambiguous": tuple(sorted(strategies)),
            "blocked": (),
            "strategies": strategies,
        }

    @_serialized
    def crescendo_state(
        self, schedule_id: str, assignment_id: str, current_attempt_id: str
    ) -> dict[str, object] | None:
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignment = next(
            (
                item
                for item in schedule["graph"].get("assignments", ())
                if isinstance(item, Mapping)
                and item.get("id") == assignment_id
                and item.get("strategy_id") == "crescendo"
            ),
            None,
        )
        if not isinstance(assignment, Mapping):
            return None
        self._reconcile_crescendo_assignment(schedule_id, assignment)
        steps = self.connection.execute(
            """SELECT step_index, stage, prompt_digest, exchange_digest, effect_id,
                      observation_digests, decision, target_session_id, contract_digest,
                      semantic_technique_id
               FROM merlin_storage.crescendo_step_receipts
               WHERE schedule_id = %s AND assignment_id = %s ORDER BY step_index""",
            (schedule_id, assignment_id),
        ).fetchall()
        terminal = self.connection.execute(
            """SELECT terminal_reason FROM merlin_storage.crescendo_terminal_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        if not steps:
            effect = self.connection.execute(
                """SELECT 1 FROM merlin_storage.tool_effects effect
                   JOIN merlin_storage.scheduler_attempts attempt
                     ON attempt.id = effect.attempt_id
                   WHERE attempt.schedule_id = %s AND attempt.node_id = %s LIMIT 1""",
                (schedule_id, f"worker:{assignment_id}"),
            ).fetchone()
            if effect is not None and terminal is None:
                return None
            raw_contract = assignment.get("strategy_contract", ())
            try:
                reviewed = {str(key): value for key, value in raw_contract}  # type: ignore[misc]
                contract = {
                    key: reviewed[key]
                    for key in (
                        "version",
                        "semantic_technique_id",
                        "objective_oracle_digest",
                        "refusal_oracle_digest",
                        "progress_oracle_digest",
                        "invalid_premise_oracle_digest",
                        "reviewed_adapter_digest",
                        "modality",
                        "language",
                        "max_target_turns",
                        "plateau_turns",
                    )
                }
                contract["max_target_turns"] = int(contract["max_target_turns"])
                contract["plateau_turns"] = int(contract["plateau_turns"])
            except (TypeError, ValueError, KeyError):
                return None
            return {
                "schema": "merlin.crescendo-state.v1",
                "contract_digest": _qualified_digest(contract),
                "semantic_technique_id": contract.get("semantic_technique_id", ""),
                "attempt_id": current_attempt_id,
                "target_session_id": None,
                "next_stage": "probe",
                "no_progress_turns": 0,
                "steps": (),
                "terminal_reason": (None if terminal is None else terminal["terminal_reason"]),
                "recovery": {
                    "decision": (
                        "not-started" if terminal is None else terminal["terminal_reason"]
                    ),
                    "exchange_digest": None,
                },
            }
        no_progress = 0
        next_stage = "probe"
        serialized_steps = []
        for row in steps:
            decision = str(row["decision"])
            no_progress = 0 if decision == "progress" else no_progress + (decision == "no-progress")
            next_stage = "backtrack" if decision == "refusal" else "escalate"
            serialized_steps.append(
                {
                    "index": row["step_index"],
                    "stage": row["stage"],
                    "prompt_arguments_digest": row["prompt_digest"],
                    "exchange_digest": row["exchange_digest"],
                    "observation_digests": tuple(row["observation_digests"]),
                    "decision": decision,
                }
            )
        return {
            "schema": "merlin.crescendo-state.v1",
            "contract_digest": steps[0]["contract_digest"],
            "semantic_technique_id": steps[0]["semantic_technique_id"],
            "attempt_id": current_attempt_id,
            "target_session_id": steps[-1]["target_session_id"],
            "next_stage": next_stage,
            "no_progress_turns": no_progress,
            "steps": serialized_steps,
            "terminal_reason": None if terminal is None else terminal["terminal_reason"],
            "recovery": {
                "decision": steps[-1]["decision"],
                "exchange_digest": steps[-1]["exchange_digest"],
            },
        }

    @_serialized
    def goat_gate(self, schedule_id: str) -> dict[str, object]:
        """Reduce fixed-K GOAT execution from exact acknowledged effects."""
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignments = tuple(
            item
            for item in schedule["graph"].get("assignments", ())
            if isinstance(item, Mapping) and item.get("strategy_id") == "goat"
        )
        drifted: set[str] = set()
        for assignment in assignments:
            try:
                self._reconcile_goat_assignment(schedule_id, assignment)
            except ValueError:
                drifted.add(str(assignment.get("id", "")))
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.goat_terminal_receipts
               WHERE schedule_id = %s ORDER BY assignment_id""",
            (schedule_id,),
        ).fetchall()
        strategies = {
            str(row["assignment_id"]): _observation_only_strategy(
                dict(row), verdict_key="evidence_verdict"
            )
            for row in rows
        }
        assignment_by_id = {str(item["id"]): item for item in assignments}
        for assignment_id, strategy in strategies.items():
            assignment = assignment_by_id.get(assignment_id, {})
            strategy["technique_ids"] = tuple(assignment.get("technique_ids", ()))
            strategy["strategy_contract"] = tuple(assignment.get("strategy_contract", ()))
            strategy["steps"] = tuple(
                dict(row)
                for row in self.connection.execute(
                    """SELECT * FROM merlin_storage.goat_step_receipts
                       WHERE schedule_id = %s AND assignment_id = %s ORDER BY step_index""",
                    (schedule_id, assignment_id),
                ).fetchall()
            )
        for assignment_id in drifted:
            if assignment_id in strategies:
                strategies[assignment_id]["execution_terminal"] = "ambiguous"
                strategies[assignment_id]["execution_complete"] = False
                strategies[assignment_id]["evidence_verdict"] = "ambiguous"
        terminal = tuple(sorted(strategies))
        execution_complete = tuple(
            key for key in terminal if strategies[key].get("execution_complete") is True
        )
        return {
            "complete": execution_complete,
            "execution_complete": execution_complete,
            "positive": (),
            "negative": (),
            "ambiguous": terminal,
            "blocked": (),
            "strategies": strategies,
        }

    @_serialized
    def goat_state(
        self,
        schedule_id: str,
        assignment_id: str,
        current_attempt_id: str,
        pending_state: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        schedule = self.connection.execute(
            "SELECT graph, session_id FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignment = next(
            (
                item
                for item in schedule["graph"].get("assignments", ())
                if isinstance(item, Mapping)
                and item.get("id") == assignment_id
                and item.get("strategy_id") == "goat"
            ),
            None,
        )
        if not isinstance(assignment, Mapping):
            return None
        self._reconcile_goat_assignment(schedule_id, assignment)
        contract = _goat_contract_value(assignment)
        if contract is None:
            return None
        rows = self.connection.execute(
            """SELECT * FROM merlin_storage.goat_step_receipts
               WHERE schedule_id = %s AND assignment_id = %s ORDER BY step_index""",
            (schedule_id, assignment_id),
        ).fetchall()
        terminal = self.connection.execute(
            """SELECT execution_terminal FROM merlin_storage.goat_terminal_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        receipt_steps = tuple(
            {
                "index": row["step_index"],
                "call_id": row["call_id"],
                "telemetry_digest": row["telemetry_digest"],
                "strategy_ids": tuple(row["strategy_ids"]),
                "response_digest": row["response_digest"],
                "arguments_digest": _as_qualified_digest(row["arguments_digest"]),
                "exchange_digest": row["exchange_digest"],
                "objective_observation_digest": row["objective_observation_digest"],
                "objective_matched": row["objective_matched"],
                "plan": dict(row["strategy_plan"]),
                "target_reply": row["target_reply"],
                "target_reply_digest": row["target_reply_digest"],
                "tool_content": row["tool_content"],
            }
            for row in rows
        )
        adopted_pending: dict[str, object] = {}
        if terminal is None and isinstance(pending_state, Mapping):
            pending_call_id = pending_state.get("pending_call_id")
            pending_arguments = pending_state.get("pending_arguments")
            pending_digest = pending_state.get("pending_arguments_digest")
            checkpoint_steps = pending_state.get("steps")
            old_attempt_id = pending_state.get("attempt_id")
            replacement_lineage = self.replacement_attempt_lineage(current_attempt_id)
            normalized_checkpoint_steps = (
                tuple(
                    {
                        **dict(item),
                        "strategy_ids": tuple(item.get("strategy_ids", ())),
                    }
                    for item in checkpoint_steps
                )
                if isinstance(checkpoint_steps, Sequence)
                and not isinstance(checkpoint_steps, (str, bytes))
                and all(isinstance(item, Mapping) for item in checkpoint_steps)
                else None
            )
            effect = (
                self.connection.execute(
                    """SELECT campaign_id, agent_id, attempt_id, target_id, session_id, state
                       FROM merlin_storage.tool_effects
                       WHERE attempt_id = %s AND call_id = %s LIMIT 1""",
                    (old_attempt_id, pending_call_id),
                ).fetchone()
                if isinstance(old_attempt_id, str)
                and old_attempt_id
                and isinstance(pending_call_id, str)
                and pending_call_id
                else None
            )
            # The seal is for an effect the live attempt can never reconcile:
            # either it belongs to a replaced predecessor attempt that died
            # after INTENT, or it was committed against a rotated session the
            # reconciliation effects query cannot see (the run24 poisoned-retry
            # shape). A live attempt re-entering with its own pending effect on
            # the schedule session is still mid-turn — its acknowledge can
            # still land — so sealing here would journal an empty ambiguous
            # terminal receipt ahead of the turn's own reconcile and poison the
            # assignment permanently.
            belongs_to_replaced_predecessor = bool(replacement_lineage[:-1]) and (
                old_attempt_id == replacement_lineage[-2]
            )
            if (
                effect is not None
                and not any(row["call_id"] == pending_call_id for row in rows)
                and (
                    belongs_to_replaced_predecessor
                    or str(effect["session_id"]) != str(schedule["session_id"])
                )
            ):
                if (
                    effect["campaign_id"] != _campaign_id_from_assignment(assignment_id)
                    or effect["agent_id"] != assignment_id
                    or str(effect["attempt_id"]) != str(old_attempt_id)
                    or effect["target_id"] != contract["target_profile_id"]
                ):
                    raise ValueError("GOAT pending call collides with foreign effect ownership")
                # The inherited pending effect owns a target INTENT that never
                # produced an acknowledged step receipt (the prior attempt died
                # after INTENT). Replaying it would double-fire the target
                # effect and re-raising would poison every replacement attempt,
                # so journal a terminal ambiguous receipt and let the worker
                # exit through the restored-terminal path instead.
                self._seal_goat_intent_crossed_terminal(schedule_id, assignment, contract, rows)
                terminal = self.connection.execute(
                    """SELECT execution_terminal FROM merlin_storage.goat_terminal_receipts
                       WHERE schedule_id = %s AND assignment_id = %s""",
                    (schedule_id, assignment_id),
                ).fetchone()
            if (
                pending_state.get("schema") == "merlin.goat-state.v1"
                and isinstance(old_attempt_id, str)
                and replacement_lineage[:-1]
                and old_attempt_id == replacement_lineage[-2]
                and pending_state.get("contract_digest") == _qualified_digest(contract)
                and pending_state.get("semantic_technique_id") == contract["semantic_technique_id"]
                and normalized_checkpoint_steps == receipt_steps
                and pending_state.get("target_session_id")
                == (None if not rows else rows[-1]["target_session_id"])
                and isinstance(pending_call_id, str)
                and pending_call_id.strip()
                and isinstance(pending_arguments, Mapping)
                and set(pending_arguments) == {"input", "strategy_plan", "strategy_turn_index"}
                and pending_arguments.get("strategy_turn_index") == len(rows)
                and len(rows) < int(contract["target_turns"])
                and pending_digest == _goat_checkpoint_digest(pending_arguments)
                and effect is None
            ):
                adopted_pending = {
                    "pending_call_id": pending_call_id,
                    "pending_arguments": dict(pending_arguments),
                    "pending_arguments_digest": pending_digest,
                }
        return {
            "schema": "merlin.goat-state.v1",
            "contract_digest": _qualified_digest(contract),
            "semantic_technique_id": contract["semantic_technique_id"],
            "attempt_id": current_attempt_id,
            "target_session_id": None if not rows else rows[-1]["target_session_id"],
            "steps": receipt_steps,
            **adopted_pending,
            "terminal_reason": (
                terminal["execution_terminal"]
                if terminal is not None
                and terminal["execution_terminal"]
                in {"cancelled", "capped", "blocked", "ambiguous"}
                else None
            ),
            "recovery": {
                "decision": "not-started" if not rows else "acknowledged-target-turn",
                "exchange_digest": None if not rows else rows[-1]["exchange_digest"],
            },
        }

    def _persist_hydra_select_plan(
        self,
        schedule_id: str,
        assignment_id: str,
        state: Mapping[str, object],
    ) -> dict[str, object]:
        """Seal the daemon-derived proposal/selection plan before any target INTENT."""
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        assignment = next(
            (
                item
                for item in (schedule or {}).get("graph", {}).get("assignments", ())
                if isinstance(item, Mapping)
                and item.get("id") == assignment_id
                and item.get("strategy_id") == "hydra-select"
            ),
            None,
        )
        contract = (
            _hydra_select_contract_value(assignment) if isinstance(assignment, Mapping) else None
        )
        plan = state.get("plan")
        if contract is None or not isinstance(plan, Mapping):
            raise ValueError("Hydra-Select plan lacks a reviewed assignment contract")
        candidates = plan.get("candidates")
        selected = plan.get("selected_candidate_ids")
        raw_plan_uncertainties = plan.get("evaluation_uncertainties", ())
        plan_uncertainties = (
            [dict(item) for item in raw_plan_uncertainties if isinstance(item, Mapping)]
            if isinstance(raw_plan_uncertainties, Sequence)
            and not isinstance(raw_plan_uncertainties, (str, bytes))
            else []
        )
        if (
            state.get("schema") != "merlin.hydra-select-state.v1"
            or state.get("contract_digest") != _qualified_digest(contract)
            or not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes))
            or not all(isinstance(item, Mapping) for item in candidates)
            or not 1 <= len(candidates) <= int(contract["proposed_count"])
            or not isinstance(selected, Sequence)
            or isinstance(selected, (str, bytes))
            or not 1 <= len(selected) <= int(contract["selected_count"])
            or len(selected) > len(candidates)
            or len(set(map(str, selected))) != len(selected)
        ):
            raise ValueError("Hydra-Select plan authority is malformed")
        candidates = [dict(item) for item in candidates]
        selected = [str(item) for item in selected]
        candidate_ids = [str(item.get("candidate_id", "")) for item in candidates]
        if (
            not all(candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or not set(map(str, selected)).issubset(candidate_ids)
        ):
            raise ValueError("Hydra-Select candidate identity is malformed")
        plan_body = {
            "schema": plan.get("schema"),
            "contract_digest": plan.get("contract_digest"),
            "candidates": candidates,
            "selected_candidate_ids": selected,
        }
        extended_plan_body = {
            **plan_body,
            "evaluation_uncertainties": plan_uncertainties,
        }
        valid_plan_digests = {
            _hydra_checkpoint_digest(plan_body),
            _hydra_checkpoint_digest(extended_plan_body),
        }
        if plan.get("plan_digest") not in valid_plan_digests:
            raise ValueError("Hydra-Select plan digest drifted")
        receipt = {
            "schedule_id": schedule_id,
            "assignment_id": assignment_id,
            "semantic_technique_id": assignment.get("semantic_technique_id"),
            "contract_digest": state.get("contract_digest"),
            "plan_digest": plan.get("plan_digest"),
            "candidates": candidates,
            "selected_candidate_ids": selected,
            "evaluation_uncertainties": plan_uncertainties,
        }
        receipt_digest = _qualified_digest(receipt)
        self.connection.execute(
            """INSERT INTO merlin_storage.hydra_select_plan_receipts
                   (schedule_id, assignment_id, semantic_technique_id, contract_digest,
                    plan_digest, candidates, selected_candidate_ids, receipt, receipt_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                schedule_id,
                assignment_id,
                receipt["semantic_technique_id"],
                receipt["contract_digest"],
                receipt["plan_digest"],
                Jsonb(candidates),
                Jsonb(selected),
                Jsonb(receipt),
                receipt_digest,
            ),
        )
        stored = self.connection.execute(
            """SELECT receipt, receipt_digest
               FROM merlin_storage.hydra_select_plan_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        if (
            stored is None
            or stored["receipt_digest"] != receipt_digest
            or dict(stored["receipt"]) != receipt
        ):
            raise ValueError("Hydra-Select immutable plan receipt conflicts")
        return dict(stored["receipt"])

    @_serialized
    def hydra_select_state(
        self,
        schedule_id: str,
        assignment_id: str,
        current_attempt_id: str,
        pending_state: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Adopt a persisted Hydra plan and exact acknowledged selected effects."""
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignment = next(
            (
                item
                for item in schedule["graph"].get("assignments", ())
                if isinstance(item, Mapping)
                and item.get("id") == assignment_id
                and item.get("strategy_id") == "hydra-select"
            ),
            None,
        )
        if not isinstance(assignment, Mapping) or not isinstance(pending_state, Mapping):
            return None
        contract = _hydra_select_contract_value(assignment)
        plan = pending_state.get("plan")
        if contract is None or not isinstance(plan, Mapping):
            return None
        if (
            pending_state.get("schema") != "merlin.hydra-select-state.v1"
            or pending_state.get("contract_digest") != _qualified_digest(contract)
            or pending_state.get("semantic_technique_id") != contract["semantic_technique_id"]
        ):
            raise ValueError("Hydra-Select checkpoint authority drifted")
        selected_ids = plan.get("selected_candidate_ids")
        candidates = plan.get("candidates")
        if (
            not isinstance(selected_ids, Sequence)
            or isinstance(selected_ids, (str, bytes))
            or not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes))
        ):
            raise ValueError("Hydra-Select checkpoint plan is malformed")
        by_id = {
            str(item.get("candidate_id")): item
            for item in candidates
            if isinstance(item, Mapping) and item.get("candidate_id")
        }
        effects = self.connection.execute(
            """SELECT effect.* FROM merlin_storage.tool_effects effect
               JOIN merlin_storage.scheduler_attempts attempt ON attempt.id = effect.attempt_id
               JOIN merlin_storage.scheduler_schedules live ON live.id = attempt.schedule_id
               WHERE attempt.schedule_id = %s AND attempt.node_id = %s
                 AND effect.agent_id = %s AND effect.target_id = %s
                 AND effect.session_id = live.session_id
                 AND effect.campaign_id = %s
                 AND effect.tool_name = 'target_request'
               ORDER BY effect.call_id, effect.idempotency_key""",
            (
                schedule_id,
                f"worker:{assignment_id}",
                assignment_id,
                assignment["target_profile_id"],
                _campaign_id_from_assignment(assignment_id),
            ),
        ).fetchall()
        steps: list[dict[str, object]] = []
        raw_state_uncertainties = pending_state.get("evaluation_uncertainties", ())
        evaluation_uncertainties = (
            [dict(item) for item in raw_state_uncertainties if isinstance(item, Mapping)]
            if isinstance(raw_state_uncertainties, Sequence)
            and not isinstance(raw_state_uncertainties, (str, bytes))
            else []
        )
        target_session_id: str | None = None
        crossed_intent = False
        blocking_effect_id: str | None = None
        blocking_effect_attempt_id: str | None = None
        for index, effect in enumerate(effects):
            if index >= len(selected_ids) or effect["state"] in {"intent", "ambiguous"}:
                crossed_intent = True
                blocking_effect_id = str(effect["idempotency_key"])
                blocking_effect_attempt_id = str(effect["attempt_id"])
                break
            candidate_id = str(selected_ids[index])
            candidate = by_id.get(candidate_id)
            metadata = effect["result_metadata"] or {}
            exchanges = metadata.get("target_exchanges")
            interactions = [
                item
                for item in exchanges or ()
                if isinstance(item, Mapping)
                and item.get("interaction") is True
                and item.get("cleanup") is not True
            ]
            session = metadata.get("target_session_id")
            observations = (
                interactions[0].get("oracle_observations") if len(interactions) == 1 else None
            )
            objective = [
                item
                for item in observations or ()
                if isinstance(item, Mapping) and item.get("purpose") == "objective"
            ]
            objective_observation = objective[0] if len(objective) == 1 else None
            observation_uncertainties: list[str] = []
            if not isinstance(observations, list):
                observation_uncertainties.append("oracle observations missing")
            elif any(
                not isinstance(item, Mapping) or not _valid_adaptive_observation(item)
                for item in observations
            ):
                observation_uncertainties.append("oracle observation integrity is uncertain")
            if not isinstance(objective_observation, Mapping):
                observation_uncertainties.append("objective observation missing or ambiguous")
            elif (
                objective_observation.get("oracle_digest") != contract["objective_oracle_digest"]
                or objective_observation.get("adapter_digest")
                != contract["reviewed_adapter_digest"]
                or objective_observation.get("modality") != contract["modality"]
                or objective_observation.get("turn") != "multi"
                or objective_observation.get("language") != contract["language"]
            ):
                observation_uncertainties.append("oracle metadata differs from the assignment")
            for reason in observation_uncertainties:
                uncertainty = {
                    "kind": "evaluation_uncertain",
                    "source": "hydra.oracle",
                    "reason": reason,
                    "confidence": 0.0,
                    "affects": ["ranking", "evaluation"],
                }
                if uncertainty not in evaluation_uncertainties:
                    evaluation_uncertainties.append(uncertainty)
            if (
                effect["state"] not in {"acknowledged", "reconciled"}
                or effect["result_is_error"] is not False
                or candidate is None
                or effect["arguments_payload"] != {"input": candidate.get("prompt")}
                or not isinstance(session, str)
                or target_session_id not in {None, session}
                or len(interactions) != 1
                or not isinstance(interactions[0].get("id"), (str, int))
                or str(interactions[0]["id"]).strip() == ""
            ):
                crossed_intent = True
                blocking_effect_id = str(effect["idempotency_key"])
                blocking_effect_attempt_id = str(effect["attempt_id"])
                break
            target_session_id = session
            exchange = interactions[0]
            steps.append(
                {
                    "index": index,
                    "scheduler_attempt_id": str(effect["attempt_id"]),
                    "effect_id": effect["idempotency_key"],
                    "candidate_id": candidate_id,
                    "call_id": effect["call_id"],
                    "arguments_digest": _as_qualified_digest(effect["arguments_digest"]),
                    "exchange_digest": _qualified_digest(exchange),
                    "exchange_id": f"{effect['call_id']}:{exchange['id']}",
                    "objective_observation_digest": (
                        objective_observation.get("observation_digest")
                        if isinstance(objective_observation, Mapping)
                        and isinstance(objective_observation.get("observation_digest"), str)
                        else _qualified_digest(
                            {
                                "effect_id": effect["idempotency_key"],
                                "evaluation_uncertain": observation_uncertainties,
                            }
                        )
                    ),
                    "objective_matched": (
                        objective_observation.get("matched") is True
                        if isinstance(objective_observation, Mapping)
                        and type(objective_observation.get("matched")) is bool
                        else None
                    ),
                    "target_reply_digest": _qualified_digest(exchange.get("response")),
                    "tool_content": effect["result_content"] or "",
                }
            )
        pending_terminal = pending_state.get("terminal_reason")
        terminal_reason = (
            pending_terminal
            if pending_terminal in {"cancelled", "capped", "blocked", "ambiguous"}
            else None
        )
        if crossed_intent:
            terminal_reason = "ambiguous"
        return {
            **dict(pending_state),
            "attempt_id": current_attempt_id,
            "target_session_id": target_session_id,
            "steps": steps,
            "pending_call_id": None,
            "pending_arguments": None,
            "pending_arguments_digest": None,
            "terminal_reason": terminal_reason,
            "blocking_effect_id": blocking_effect_id,
            "blocking_effect_attempt_id": blocking_effect_attempt_id,
            "evaluation_uncertainties": evaluation_uncertainties,
            "recovery": {
                "decision": (
                    "ambiguous-intent-no-resend"
                    if crossed_intent
                    else "persisted-plan"
                    if not steps
                    else "acknowledged-selected-effect"
                ),
                "exchange_digest": None if not steps else steps[-1]["exchange_digest"],
            },
        }

    @_serialized
    def hydra_select_gate(self, schedule_id: str) -> dict[str, object]:
        """Reduce Hydra-Select evidence from persisted plans and acknowledged effects."""
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignments = tuple(
            item
            for item in schedule["graph"].get("assignments", ())
            if isinstance(item, Mapping) and item.get("strategy_id") == "hydra-select"
        )
        strategies: dict[str, dict[str, object]] = {}
        for assignment in assignments:
            assignment_id = str(assignment["id"])
            attempt = self.connection.execute(
                """SELECT id FROM merlin_storage.scheduler_attempts
                   WHERE schedule_id = %s AND node_id = %s
                   ORDER BY generation DESC, started_at DESC LIMIT 1""",
                (schedule_id, f"worker:{assignment_id}"),
            ).fetchone()
            checkpoint = self.connection.execute(
                """SELECT payload->'worker_checkpoint' AS checkpoint
                   FROM merlin_storage.scheduler_results
                   WHERE schedule_id = %s AND node_id = %s
                     AND payload ? 'worker_checkpoint'
                     AND payload->'worker_checkpoint' <> 'null'::jsonb
                   ORDER BY created_at DESC LIMIT 1""",
                (schedule_id, f"worker:{assignment_id}"),
            ).fetchone()
            state = (
                None
                if attempt is None or checkpoint is None
                else self.hydra_select_state(
                    schedule_id,
                    assignment_id,
                    str(attempt["id"]),
                    (checkpoint["checkpoint"] or {}).get("strategy_state"),
                )
            )
            if state is None or not isinstance(state.get("plan"), Mapping):
                node = self.connection.execute(
                    """SELECT state FROM merlin_storage.scheduler_nodes
                       WHERE schedule_id = %s AND node_id = %s""",
                    (schedule_id, f"worker:{assignment_id}"),
                ).fetchone()
                durable_terminal = node is not None and node["state"] in {
                    "completed",
                    "failed",
                    "cancelled",
                    "quarantined",
                }
                missing_digest = _qualified_digest(
                    {
                        "schema": "merlin.hydra-select-no-plan.v1",
                        "schedule_id": schedule_id,
                        "assignment_id": assignment_id,
                        "node_state": None if node is None else node["state"],
                    }
                )
                if durable_terminal:
                    no_plan_receipt = {
                        "schema": "merlin.hydra-select-terminal.v1",
                        "schedule_id": schedule_id,
                        "assignment_id": assignment_id,
                        "terminal_attempt_id": (None if attempt is None else str(attempt["id"])),
                        "blocking_effect_attempt_id": None,
                        "blocking_effect_id": None,
                        "semantic_technique_id": assignment.get("semantic_technique_id"),
                        "contract_digest": _qualified_digest(
                            assignment.get("strategy_contract", ())
                        ),
                        "plan_digest": missing_digest,
                        "target_session_id": None,
                        "execution_complete": False,
                        "execution_terminal": (
                            "cancelled" if node["state"] == "cancelled" else "ambiguous"
                        ),
                        "evidence_verdict": "ambiguous",
                        "step_receipt_digests": (),
                    }
                    no_plan_digest = _qualified_digest(no_plan_receipt)
                    self.connection.execute(
                        """INSERT INTO merlin_storage.hydra_select_terminal_receipts
                               (schedule_id, assignment_id, terminal_attempt_id,
                                blocking_effect_attempt_id, blocking_effect_id,
                                semantic_technique_id,
                                contract_digest, plan_digest, target_session_id,
                                execution_complete, execution_terminal, evidence_verdict,
                                step_receipt_digests, receipt, receipt_digest)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""",
                        (
                            schedule_id,
                            assignment_id,
                            no_plan_receipt["terminal_attempt_id"],
                            None,
                            None,
                            no_plan_receipt["semantic_technique_id"],
                            no_plan_receipt["contract_digest"],
                            missing_digest,
                            None,
                            False,
                            no_plan_receipt["execution_terminal"],
                            "ambiguous",
                            Jsonb([]),
                            Jsonb(no_plan_receipt),
                            no_plan_digest,
                        ),
                    )
                    stored_no_plan = self.connection.execute(
                        """SELECT receipt, receipt_digest
                           FROM merlin_storage.hydra_select_terminal_receipts
                           WHERE schedule_id = %s AND assignment_id = %s""",
                        (schedule_id, assignment_id),
                    ).fetchone()
                    if (
                        stored_no_plan is None
                        or stored_no_plan["receipt_digest"] != no_plan_digest
                        or _qualified_digest(dict(stored_no_plan["receipt"])) != no_plan_digest
                    ):
                        raise ValueError("Hydra-Select no-plan terminal receipt conflicts")
                strategies[assignment_id] = {
                    **(
                        dict(stored_no_plan["receipt"])
                        if durable_terminal
                        else {
                            "assignment_id": assignment_id,
                            "semantic_technique_id": assignment.get("semantic_technique_id"),
                            "execution_complete": False,
                            "execution_terminal": "ambiguous",
                            "evidence_verdict": "ambiguous",
                        }
                    ),
                    "technique_ids": tuple(assignment.get("technique_ids", ())),
                    "receipt_digest": (
                        stored_no_plan["receipt_digest"] if durable_terminal else missing_digest
                    ),
                    "_durable_terminal": durable_terminal,
                    "candidates": (),
                    "steps": (),
                }
                continue
            plan = dict(state["plan"])
            steps = tuple(dict(item) for item in state.get("steps", ()))
            raw_plan_uncertainties = plan.get("evaluation_uncertainties", ())
            plan_receipt = {
                "schedule_id": schedule_id,
                "assignment_id": assignment_id,
                "semantic_technique_id": assignment.get("semantic_technique_id"),
                "contract_digest": state.get("contract_digest"),
                "plan_digest": plan.get("plan_digest"),
                "candidates": tuple(dict(item) for item in plan.get("candidates", ())),
                "selected_candidate_ids": tuple(plan.get("selected_candidate_ids", ())),
                "evaluation_uncertainties": tuple(
                    dict(item)
                    for item in (
                        raw_plan_uncertainties
                        if isinstance(raw_plan_uncertainties, Sequence)
                        and not isinstance(raw_plan_uncertainties, (str, bytes))
                        else ()
                    )
                    if isinstance(item, Mapping)
                ),
            }
            plan_receipt_digest = _qualified_digest(plan_receipt)
            self.connection.execute(
                """INSERT INTO merlin_storage.hydra_select_plan_receipts
                       (schedule_id, assignment_id, semantic_technique_id, contract_digest,
                        plan_digest, candidates, selected_candidate_ids, receipt, receipt_digest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    schedule_id,
                    assignment_id,
                    plan_receipt["semantic_technique_id"],
                    plan_receipt["contract_digest"],
                    plan_receipt["plan_digest"],
                    Jsonb(list(plan_receipt["candidates"])),
                    Jsonb(list(plan_receipt["selected_candidate_ids"])),
                    Jsonb(plan_receipt),
                    plan_receipt_digest,
                ),
            )
            stored_plan = self.connection.execute(
                """SELECT receipt, receipt_digest
                   FROM merlin_storage.hydra_select_plan_receipts
                   WHERE schedule_id = %s AND assignment_id = %s""",
                (schedule_id, assignment_id),
            ).fetchone()
            if stored_plan is None or stored_plan["receipt_digest"] != plan_receipt_digest:
                raise ValueError("Hydra-Select immutable plan receipt conflicts")
            step_receipt_digests: list[str] = []
            for item in steps:
                step_receipt = {
                    "schedule_id": schedule_id,
                    "assignment_id": assignment_id,
                    **item,
                }
                step_receipt_digest = _qualified_digest(step_receipt)
                step_receipt_digests.append(step_receipt_digest)
                self.connection.execute(
                    """INSERT INTO merlin_storage.hydra_select_step_receipts
                           (schedule_id, assignment_id, step_index, scheduler_attempt_id,
                            effect_id, candidate_id, call_id, target_session_id, arguments_digest,
                            exchange_id, exchange_digest, objective_observation_digest,
                            objective_matched, receipt, receipt_digest)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (
                        schedule_id,
                        assignment_id,
                        item["index"],
                        item["scheduler_attempt_id"],
                        item["effect_id"],
                        item["candidate_id"],
                        item["call_id"],
                        state["target_session_id"],
                        item["arguments_digest"],
                        item["exchange_id"],
                        item["exchange_digest"],
                        item["objective_observation_digest"],
                        item["objective_matched"],
                        Jsonb(step_receipt),
                        step_receipt_digest,
                    ),
                )
                stored_step = self.connection.execute(
                    """SELECT receipt_digest FROM merlin_storage.hydra_select_step_receipts
                       WHERE schedule_id = %s AND assignment_id = %s AND step_index = %s""",
                    (schedule_id, assignment_id, item["index"]),
                ).fetchone()
                if stored_step is None or stored_step["receipt_digest"] != step_receipt_digest:
                    raise ValueError("Hydra-Select immutable step receipt conflicts")
            immutable_steps = tuple(
                {
                    **dict(row["receipt"]),
                    "receipt_digest": row["receipt_digest"],
                }
                for row in self.connection.execute(
                    """SELECT receipt, receipt_digest
                       FROM merlin_storage.hydra_select_step_receipts
                       WHERE schedule_id = %s AND assignment_id = %s
                       ORDER BY step_index""",
                    (schedule_id, assignment_id),
                ).fetchall()
            )
            selected_count = len(tuple(plan.get("selected_candidate_ids", ())))
            complete = len(steps) == selected_count and state.get("blocking_effect_id") is None
            verdict = "ambiguous"
            raw_state_uncertainties = state.get("evaluation_uncertainties", ())
            receipt = {
                "schema": "merlin.hydra-select-terminal.v1",
                "schedule_id": schedule_id,
                "assignment_id": assignment_id,
                "terminal_attempt_id": str(attempt["id"]),
                "blocking_effect_attempt_id": state.get("blocking_effect_attempt_id"),
                "semantic_technique_id": assignment.get("semantic_technique_id"),
                "contract_digest": state.get("contract_digest"),
                "plan_digest": plan.get("plan_digest"),
                "target_session_id": state.get("target_session_id"),
                "execution_complete": complete,
                "execution_terminal": state.get("terminal_reason"),
                "evidence_verdict": verdict,
                "step_receipt_digests": tuple(step_receipt_digests),
                "blocking_effect_id": state.get("blocking_effect_id"),
                "evaluation_uncertainties": tuple(
                    dict(item)
                    for item in (
                        raw_state_uncertainties
                        if isinstance(raw_state_uncertainties, Sequence)
                        and not isinstance(raw_state_uncertainties, (str, bytes))
                        else ()
                    )
                    if isinstance(item, Mapping)
                ),
            }
            node = self.connection.execute(
                """SELECT state FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = %s""",
                (schedule_id, f"worker:{assignment_id}"),
            ).fetchone()
            durable_terminal = node is not None and node["state"] in {
                "completed",
                "failed",
                "cancelled",
                "quarantined",
            }
            if receipt["execution_terminal"] is None or not durable_terminal:
                strategies[assignment_id] = {
                    **receipt,
                    "evidence_verdict": "ambiguous",
                    "receipt_digest": plan_receipt_digest,
                    "plan_receipt_digest": plan_receipt_digest,
                    "technique_ids": tuple(assignment.get("technique_ids", ())),
                    "strategy_contract": tuple(assignment.get("strategy_contract", ())),
                    "candidates": tuple(dict(item) for item in plan.get("candidates", ())),
                    "selected_candidate_ids": tuple(plan.get("selected_candidate_ids", ())),
                    "steps": immutable_steps,
                    "requested_mechanism_conformance": "unverified",
                    "independent_reproduction": "not-established",
                }
                continue
            terminal_digest = _qualified_digest(receipt)
            self.connection.execute(
                """INSERT INTO merlin_storage.hydra_select_terminal_receipts
                       (schedule_id, assignment_id, terminal_attempt_id,
                        blocking_effect_attempt_id, blocking_effect_id,
                        semantic_technique_id, contract_digest,
                        plan_digest, target_session_id, execution_complete,
                        execution_terminal, evidence_verdict, step_receipt_digests,
                        receipt, receipt_digest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    schedule_id,
                    assignment_id,
                    receipt["terminal_attempt_id"],
                    receipt["blocking_effect_attempt_id"],
                    receipt["blocking_effect_id"],
                    receipt["semantic_technique_id"],
                    receipt["contract_digest"],
                    receipt["plan_digest"],
                    receipt["target_session_id"],
                    receipt["execution_complete"],
                    receipt["execution_terminal"],
                    receipt["evidence_verdict"],
                    Jsonb(list(receipt["step_receipt_digests"])),
                    Jsonb(receipt),
                    terminal_digest,
                ),
            )
            stored_terminal = self.connection.execute(
                """SELECT receipt, receipt_digest
                   FROM merlin_storage.hydra_select_terminal_receipts
                   WHERE schedule_id = %s AND assignment_id = %s""",
                (schedule_id, assignment_id),
            ).fetchone()
            if (
                stored_terminal is None
                or stored_terminal["receipt_digest"] != terminal_digest
                or _qualified_digest(dict(stored_terminal["receipt"])) != terminal_digest
            ):
                raise ValueError("Hydra-Select immutable terminal receipt conflicts")
            strategies[assignment_id] = {
                **dict(stored_terminal["receipt"]),
                "receipt_digest": stored_terminal["receipt_digest"],
                "_durable_terminal": True,
                "plan_receipt_digest": stored_plan["receipt_digest"],
                "technique_ids": tuple(assignment.get("technique_ids", ())),
                "strategy_contract": tuple(assignment.get("strategy_contract", ())),
                "candidates": tuple(dict(item) for item in plan.get("candidates", ())),
                "selected_candidate_ids": tuple(plan.get("selected_candidate_ids", ())),
                "steps": immutable_steps,
                "requested_mechanism_conformance": "unverified",
                "independent_reproduction": "not-established",
            }
        strategies = {
            key: _observation_only_strategy(value, verdict_key="evidence_verdict")
            for key, value in strategies.items()
        }
        complete_ids = tuple(
            sorted(
                key for key, item in strategies.items() if item.get("execution_complete") is True
            )
        )
        return {
            "complete": complete_ids,
            "execution_complete": complete_ids,
            "positive": (),
            "negative": (),
            "ambiguous": tuple(sorted(strategies)),
            "blocked": (),
            "strategies": strategies,
        }

    def _seal_goat_intent_crossed_terminal(
        self,
        schedule_id: str,
        assignment: Mapping[str, object],
        contract: Mapping[str, Any],
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Journal a terminal receipt for a pending target effect that crossed
        INTENT without an acknowledged step receipt.

        The digest recipe matches ``_reconcile_goat_assignment`` exactly so a
        later reconciliation re-verifies this receipt instead of conflicting.
        """
        assignment_id = str(assignment["id"])
        terminal_value = {
            "schedule_id": schedule_id,
            "assignment_id": assignment_id,
            "semantic_technique_id": contract["semantic_technique_id"],
            "contract_digest": _qualified_digest(contract),
            "adapter_digest": contract["reviewed_adapter_digest"],
            "target_session_id": None if not rows else rows[-1]["target_session_id"],
            "target_turns": int(contract["target_turns"]),
            "execution_terminal": "ambiguous",
            "execution_complete": False,
            "evidence_verdict": "ambiguous",
            "step_receipt_digests": [row["receipt_digest"] for row in rows],
        }
        receipt_digest = _qualified_digest(terminal_value)
        self.connection.execute(
            """INSERT INTO merlin_storage.goat_terminal_receipts
                   (schedule_id, assignment_id, semantic_technique_id, contract_digest,
                    adapter_digest, target_session_id, target_turns, execution_terminal,
                    execution_complete, evidence_verdict, block_reason,
                    step_receipt_digests, receipt_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                schedule_id,
                assignment_id,
                contract["semantic_technique_id"],
                terminal_value["contract_digest"],
                contract["reviewed_adapter_digest"],
                terminal_value["target_session_id"],
                terminal_value["target_turns"],
                "ambiguous",
                False,
                terminal_value["evidence_verdict"],
                # block_reason is constrained to blocked terminals.
                None,
                Jsonb(terminal_value["step_receipt_digests"]),
                receipt_digest,
            ),
        )
        stored = self.connection.execute(
            """SELECT receipt_digest FROM merlin_storage.goat_terminal_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        if stored is None or stored["receipt_digest"] != receipt_digest:
            raise ValueError("GOAT terminal receipt conflicts with effect lineage")

    def _reconcile_goat_assignment(
        self, schedule_id: str, assignment: Mapping[str, object]
    ) -> None:
        assignment_id = str(assignment["id"])
        contract = _goat_contract_value(assignment)
        if contract is None:
            return
        expected_route_digest = _qualified_digest(
            {
                "provider_id": str(assignment.get("provider_id") or ""),
                "model": str(assignment.get("provider_model") or ""),
                "role": "worker",
                "tool_transport": "buffered",
            }
        )
        if contract["attacker_route_digest"] != expected_route_digest:
            raise ValueError("GOAT assignment route differs from its sealed attacker route")
        expected_campaign = _campaign_id_from_assignment(assignment_id)
        tool_candidates = (
            ("browser_chat_exchange", "target_request")
            if "browser_chat_exchange" in assignment.get("tool_ids", ())
            else ("target_request",)
        )
        effects = self.connection.execute(
            """SELECT effect.* FROM merlin_storage.tool_effects effect
               JOIN merlin_storage.scheduler_attempts attempt ON attempt.id = effect.attempt_id
               JOIN merlin_storage.scheduler_schedules schedule ON schedule.id = attempt.schedule_id
               WHERE attempt.schedule_id = %s AND attempt.node_id = %s
                 AND effect.session_id = schedule.session_id
                 AND effect.agent_id = %s AND effect.target_id = %s
                 AND effect.tool_name = ANY(%s)
               ORDER BY CASE
                   WHEN effect.arguments_payload->>'strategy_turn_index' ~ '^[0-9]+$'
                   THEN (effect.arguments_payload->>'strategy_turn_index')::integer
                   ELSE 2147483647
               END, effect.idempotency_key""",
            (
                schedule_id,
                f"worker:{assignment_id}",
                assignment_id,
                assignment.get("target_profile_id"),
                list(tool_candidates),
            ),
        ).fetchall()
        # Admitted goat toolboxes may list both target transports; the committed
        # effects, not the admission list, decide which transport the worker
        # actually used. Only a run with no effects at all falls back to the
        # admission heuristic (browser effects can acknowledge late).
        expected_tool = (
            "browser_chat_exchange"
            if any(effect["tool_name"] == "browser_chat_exchange" for effect in effects)
            else "target_request"
            if effects
            else tool_candidates[0]
        )
        target_session: str | None = None
        step_digests: list[str] = []
        invalid = False
        pending_ack = False
        exhausted_digest: str | None = None
        target_turns = int(contract["target_turns"])
        toolbox_ids = tuple(name for name, _digest, _definition in contract["toolbox"])
        for index, effect in enumerate(effects):
            if index >= target_turns:
                invalid = True
                break
            payload = effect["arguments_payload"]
            metadata = effect["result_metadata"] or {}
            exchanges = metadata.get("target_exchanges")
            interactions = (
                [
                    item
                    for item in exchanges
                    if isinstance(item, Mapping)
                    and item.get("interaction") is True
                    and item.get("cleanup") is not True
                ]
                if isinstance(exchanges, list)
                else []
            )
            session = metadata.get("target_session_id")
            plan = payload.get("strategy_plan") if isinstance(payload, Mapping) else None
            prompt_key = "prompt" if expected_tool == "browser_chat_exchange" else "input"
            if (
                effect["state"] not in {"acknowledged", "reconciled"}
                or effect["result_is_error"] is not False
                or effect["campaign_id"] != expected_campaign
                or (
                    expected_tool == "target_request"
                    and metadata.get("campaign_id") != expected_campaign
                )
                or (
                    expected_tool == "target_request"
                    and metadata.get("target_profile_id") != contract["target_profile_id"]
                )
                or not isinstance(session, str)
                or target_session not in {None, session}
                or len(interactions) != 1
                or not isinstance(payload, Mapping)
                or set(payload) != {prompt_key, "strategy_plan", "strategy_turn_index"}
                or payload.get("strategy_turn_index") != index
                or not _valid_goat_plan(plan, toolbox_ids)
                or payload.get(prompt_key) != plan.get("response")  # type: ignore[union-attr]
            ):
                invalid = True
                # An effect that has not acknowledged yet is in flight, not
                # invalid evidence — but only while the attempt that owns it is
                # still alive: its acknowledge can land on the next turn, so
                # the caller must defer instead of sealing an ambiguous
                # terminal receipt ahead of the turn's own reconcile. An
                # unacknowledged effect whose owning attempt already expired or
                # failed is a genuine intent-crossed death and seals as before.
                if effect["state"] not in {"acknowledged", "reconciled"}:
                    owner = self.connection.execute(
                        """SELECT state FROM merlin_storage.scheduler_attempts
                           WHERE id = %s""",
                        (effect["attempt_id"],),
                    ).fetchone()
                    pending_ack = owner is not None and owner["state"] == "running"
                if metadata.get("target_session_exhausted") is True:
                    # The fail-closed adapter error is a terminally exhausted
                    # target session (error/rate-limit envelope persisted across
                    # one session rotation), not evidence ambiguity.
                    digest = metadata.get("response_digest")
                    exhausted_digest = digest if isinstance(digest, str) else ""
                break
            observations = interactions[0].get("oracle_observations")
            observation = (
                [
                    item
                    for item in observations
                    if isinstance(item, Mapping) and item.get("purpose") == "objective"
                ]
                if isinstance(observations, list)
                else []
            )
            objective_observation = observation[0] if len(observation) == 1 else None
            observation_uncertain = (
                not isinstance(observations, list)
                or any(
                    not isinstance(item, Mapping) or not _valid_adaptive_observation(item)
                    for item in observations or ()
                )
                or not isinstance(objective_observation, Mapping)
                or objective_observation.get("purpose") != "objective"
                or objective_observation.get("oracle_digest") != contract["terminal_oracle_digest"]
                or objective_observation.get("adapter_digest")
                != contract["reviewed_adapter_digest"]
                or objective_observation.get("modality") != contract["modality"]
                or objective_observation.get("turn") != "multi"
                or objective_observation.get("language") != contract["language"]
            )
            target_session = session
            strategies = tuple(str(item) for item in plan["strategy"])  # type: ignore[index]
            telemetry = {
                "observation": plan["observation"],  # type: ignore[index]
                "thought": plan["thought"],  # type: ignore[index]
                "strategies": strategies,
            }
            response_digest = _goat_checkpoint_digest(plan["response"])  # type: ignore[index]
            target_reply = interactions[0].get("response")
            target_reply_digest = _goat_checkpoint_digest(target_reply)
            receipt = {
                "schedule_id": schedule_id,
                "assignment_id": assignment_id,
                "step_index": index,
                "scheduler_attempt_id": str(effect["attempt_id"]),
                "effect_id": effect["idempotency_key"],
                "semantic_technique_id": contract["semantic_technique_id"],
                "contract_digest": _qualified_digest(contract),
                "adapter_digest": contract["reviewed_adapter_digest"],
                "call_id": effect["call_id"],
                "target_session_id": session,
                "arguments_digest": effect["arguments_digest"],
                "response_digest": response_digest,
                "telemetry_digest": _goat_checkpoint_digest(telemetry),
                "strategy_ids": strategies,
                "exchange_id": f"{effect['call_id']}:{interactions[0].get('id', 0)}",
                "exchange_digest": _goat_checkpoint_digest(interactions[0]),
                "objective_observation_digest": (
                    objective_observation.get("observation_digest")
                    if isinstance(objective_observation, Mapping)
                    and isinstance(objective_observation.get("observation_digest"), str)
                    else _qualified_digest(
                        {
                            "effect_id": effect["idempotency_key"],
                            "evaluation_uncertain": True,
                        }
                    )
                ),
                "objective_matched": (
                    objective_observation.get("matched") is True
                    if isinstance(objective_observation, Mapping) and not observation_uncertain
                    else None
                ),
                "target_reply_digest": target_reply_digest,
                "tool_content_digest": _qualified_digest(effect["result_content"] or ""),
            }
            receipt_digest = _qualified_digest(receipt)
            self.connection.execute(
                """INSERT INTO merlin_storage.goat_step_receipts
                       (schedule_id, assignment_id, step_index, scheduler_attempt_id, effect_id,
                        semantic_technique_id, contract_digest, adapter_digest, call_id,
                        target_session_id, arguments_digest, response_digest, telemetry_digest,
                        strategy_ids, exchange_id, exchange_digest,
                        objective_observation_digest, objective_matched, strategy_plan,
                        target_reply, target_reply_digest, tool_content, receipt_digest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    schedule_id,
                    assignment_id,
                    index,
                    effect["attempt_id"],
                    effect["idempotency_key"],
                    contract["semantic_technique_id"],
                    receipt["contract_digest"],
                    contract["reviewed_adapter_digest"],
                    effect["call_id"],
                    session,
                    effect["arguments_digest"],
                    response_digest,
                    receipt["telemetry_digest"],
                    Jsonb(strategies),
                    receipt["exchange_id"],
                    receipt["exchange_digest"],
                    receipt["objective_observation_digest"],
                    receipt["objective_matched"],
                    Jsonb(plan),
                    Jsonb(target_reply),
                    target_reply_digest,
                    effect["result_content"] or "",
                    receipt_digest,
                ),
            )
            stored = self.connection.execute(
                """SELECT receipt_digest FROM merlin_storage.goat_step_receipts
                   WHERE schedule_id = %s AND assignment_id = %s AND step_index = %s""",
                (schedule_id, assignment_id, index),
            ).fetchone()
            if stored is None or stored["receipt_digest"] != receipt_digest:
                raise ValueError("GOAT step receipt conflicts with acknowledged effect")
            step_digests.append(receipt_digest)
        node = self.connection.execute(
            """SELECT state FROM merlin_storage.scheduler_nodes
               WHERE schedule_id = %s AND node_id = %s""",
            (schedule_id, f"worker:{assignment_id}"),
        ).fetchone()
        execution_complete = len(step_digests) == target_turns and not invalid
        if expected_tool == "browser_chat_exchange" and not any(
            effect["state"] in {"acknowledged", "reconciled"} for effect in effects
        ):
            return
        if (
            not execution_complete
            and (not invalid or pending_ack)
            and node is not None
            and node["state"]
            not in {
                "failed",
                "cancelled",
                "completed",
                "quarantined",
            }
        ):
            return
        terminal = (
            "max-turns"
            if execution_complete
            else "blocked"
            if exhausted_digest is not None
            else "cancelled"
            if node is not None and node["state"] == "cancelled"
            else "ambiguous"
        )
        evidence = "ambiguous"
        block_reason = (
            "target session rate/message cap exhausted "
            f"(response digest {exhausted_digest or 'unavailable'})"
            if exhausted_digest is not None
            else None
        )
        terminal_value = {
            "schedule_id": schedule_id,
            "assignment_id": assignment_id,
            "semantic_technique_id": contract["semantic_technique_id"],
            "contract_digest": _qualified_digest(contract),
            "adapter_digest": contract["reviewed_adapter_digest"],
            "target_session_id": target_session,
            "target_turns": target_turns,
            "execution_terminal": terminal,
            "execution_complete": execution_complete,
            "evidence_verdict": evidence,
            "step_receipt_digests": step_digests,
        }
        receipt_digest = _qualified_digest(terminal_value)
        self.connection.execute(
            """INSERT INTO merlin_storage.goat_terminal_receipts
                   (schedule_id, assignment_id, semantic_technique_id, contract_digest,
                    adapter_digest, target_session_id, target_turns, execution_terminal,
                    execution_complete, evidence_verdict, block_reason,
                    step_receipt_digests, receipt_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                schedule_id,
                assignment_id,
                contract["semantic_technique_id"],
                terminal_value["contract_digest"],
                contract["reviewed_adapter_digest"],
                target_session,
                target_turns,
                terminal,
                execution_complete,
                evidence,
                block_reason,
                Jsonb(step_digests),
                receipt_digest,
            ),
        )
        stored = self.connection.execute(
            """SELECT receipt_digest FROM merlin_storage.goat_terminal_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        if stored is None or stored["receipt_digest"] != receipt_digest:
            raise ValueError("GOAT terminal receipt conflicts with effect lineage")

    def _reconcile_crescendo_assignment(
        self, schedule_id: str, assignment: Mapping[str, object]
    ) -> None:
        assignment_id = str(assignment["id"])
        assignment_parts = assignment_id.split("/")
        expected_campaign_id = (
            "/".join(assignment_parts[:2])
            if len(assignment_parts) >= 2 and assignment_parts[0] == "campaign"
            else assignment_parts[0]
        )
        raw_contract = assignment.get("strategy_contract")
        if not isinstance(raw_contract, Sequence) or isinstance(raw_contract, (str, bytes)):
            return
        required = {
            "version",
            "semantic_technique_id",
            "objective_oracle_digest",
            "refusal_oracle_digest",
            "progress_oracle_digest",
            "invalid_premise_oracle_digest",
            "reviewed_adapter_digest",
            "modality",
            "language",
            "max_target_turns",
            "plateau_turns",
        }
        try:
            reviewed = {str(key): value for key, value in raw_contract}  # type: ignore[misc]
            contract = {key: reviewed[key] for key in required}
        except (TypeError, ValueError, KeyError):
            return
        if contract.get("version") != "merlin.crescendo.v1":
            return
        try:
            contract["max_target_turns"] = int(contract["max_target_turns"])
            contract["plateau_turns"] = int(contract["plateau_turns"])
        except (TypeError, ValueError):
            return
        oracle_digests = {
            contract["objective_oracle_digest"],
            contract["refusal_oracle_digest"],
            contract["progress_oracle_digest"],
            contract["invalid_premise_oracle_digest"],
        }
        if (
            len(oracle_digests) != 4
            or any(
                not isinstance(item, str) or not _is_qualified_digest(item)
                for item in oracle_digests
            )
            or not isinstance(contract["reviewed_adapter_digest"], str)
            or not _is_qualified_digest(contract["reviewed_adapter_digest"])
            or not str(contract["semantic_technique_id"]).strip()
            or contract["modality"] != "text"
            or not str(contract["language"]).strip()
            or int(contract["max_target_turns"]) < 2
            or not 1 <= int(contract["plateau_turns"]) < int(contract["max_target_turns"])
        ):
            return
        effects = self.connection.execute(
            """SELECT effect.*, attempt.node_id
               FROM merlin_storage.tool_effects effect
               JOIN merlin_storage.scheduler_attempts attempt ON attempt.id = effect.attempt_id
               JOIN merlin_storage.scheduler_schedules schedule ON schedule.id = attempt.schedule_id
               WHERE attempt.schedule_id = %s AND attempt.node_id = %s
                 AND effect.session_id = schedule.session_id
                 AND effect.agent_id = %s AND effect.target_id = %s
                 AND effect.tool_name = 'target_request'
               ORDER BY effect.created_at, effect.idempotency_key""",
            (
                schedule_id,
                f"worker:{assignment_id}",
                assignment_id,
                assignment.get("target_profile_id"),
            ),
        ).fetchall()
        stage = "probe"
        step_digests: list[str] = []
        target_session: str | None = None
        terminal: str | None = None
        oracle_by_purpose = {
            "objective": contract["objective_oracle_digest"],
            "refusal": contract["refusal_oracle_digest"],
            "progress": contract["progress_oracle_digest"],
            "invalid-premise": contract["invalid_premise_oracle_digest"],
        }
        for index, effect in enumerate(effects):
            if terminal is not None:
                terminal = "ambiguous"
                break
            metadata = effect["result_metadata"] or {}
            if (
                not isinstance(effect["campaign_id"], str)
                or effect["campaign_id"] != expected_campaign_id
                or metadata.get("campaign_id") != effect["campaign_id"]
                or metadata.get("target_profile_id") != effect["target_id"]
            ):
                terminal = "ambiguous"
                break
            session = metadata.get("target_session_id")
            target_exchanges = metadata.get("target_exchanges")
            interactions = (
                [
                    item
                    for item in target_exchanges
                    if isinstance(item, Mapping)
                    and item.get("interaction") is True
                    and item.get("cleanup") is not True
                ]
                if isinstance(target_exchanges, list)
                else []
            )
            raw_observations = (
                interactions[0].get("oracle_observations") if len(interactions) == 1 else None
            )
            observations = raw_observations if isinstance(raw_observations, list) else []
            if (
                effect["state"] not in {"acknowledged", "reconciled"}
                or effect["result_is_error"] is not False
                or not isinstance(session, str)
                or (target_session is not None and session != target_session)
                or len(interactions) != 1
            ):
                terminal = "ambiguous"
                break
            target_session = session
            by_purpose: dict[str, Mapping[str, object]] = {}
            for observation in observations:
                if not isinstance(observation, Mapping) or not _valid_adaptive_observation(
                    observation
                ):
                    continue
                purpose = str(observation.get("purpose", ""))
                if purpose in by_purpose:
                    continue
                by_purpose[purpose] = observation
            conforming_by_purpose = {
                purpose: observation
                for purpose, observation in by_purpose.items()
                if purpose in oracle_by_purpose
                and observation.get("oracle_digest") == oracle_by_purpose[purpose]
                and observation.get("adapter_digest") == contract["reviewed_adapter_digest"]
                and observation.get("modality") == contract["modality"]
                and observation.get("turn") == "multi"
                and observation.get("language") == contract["language"]
            }
            matched = [
                purpose
                for purpose, observation in conforming_by_purpose.items()
                if observation.get("matched") is True
            ]
            if len(matched) > 1:
                matched = []
            decision = (
                "success"
                if matched == ["objective"]
                else "invalid-premise"
                if matched == ["invalid-premise"]
                else "refusal"
                if matched == ["refusal"]
                else "progress"
                if matched == ["progress"]
                else "no-progress"
            )
            receipt = {
                "schedule_id": schedule_id,
                "assignment_id": assignment_id,
                "semantic_technique_id": contract["semantic_technique_id"],
                "contract_digest": _qualified_digest(contract),
                "adapter_digest": contract["reviewed_adapter_digest"],
                "step_index": index,
                "stage": stage,
                "scheduler_attempt_id": str(effect["attempt_id"]),
                "effect_id": effect["idempotency_key"],
                "call_id": effect["call_id"],
                "target_session_id": session,
                "arguments_digest": effect["arguments_digest"],
                "prompt_digest": _as_qualified_digest(effect["arguments_digest"]),
                "exchange_id": f"{effect['call_id']}:{interactions[0].get('id', 0)}",
                "exchange_digest": _qualified_digest(interactions[0]),
                "observation_digests": [
                    (
                        conforming_by_purpose[purpose]["observation_digest"]
                        if purpose in conforming_by_purpose
                        else _qualified_digest(
                            {
                                "effect_id": effect["idempotency_key"],
                                "purpose": purpose,
                                "evaluation_uncertain": True,
                            }
                        )
                    )
                    for purpose in ("objective", "refusal", "progress", "invalid-premise")
                ],
                "decision": decision,
            }
            receipt_digest = _qualified_digest(receipt)
            self.connection.execute(
                """INSERT INTO merlin_storage.crescendo_step_receipts
                   (schedule_id, assignment_id, step_index, scheduler_attempt_id, effect_id,
                    semantic_technique_id, contract_digest, adapter_digest, stage,
                    call_id, target_session_id, arguments_digest, prompt_digest,
                    exchange_id, exchange_digest,
                    observation_digests, decision, receipt_digest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (
                    schedule_id,
                    assignment_id,
                    index,
                    effect["attempt_id"],
                    effect["idempotency_key"],
                    contract["semantic_technique_id"],
                    receipt["contract_digest"],
                    contract["reviewed_adapter_digest"],
                    stage,
                    effect["call_id"],
                    session,
                    effect["arguments_digest"],
                    receipt["prompt_digest"],
                    receipt["exchange_id"],
                    receipt["exchange_digest"],
                    Jsonb(receipt["observation_digests"]),
                    decision,
                    receipt_digest,
                ),
            )
            stored_step = self.connection.execute(
                """SELECT receipt_digest FROM merlin_storage.crescendo_step_receipts
                   WHERE schedule_id = %s AND assignment_id = %s AND step_index = %s""",
                (schedule_id, assignment_id, index),
            ).fetchone()
            if stored_step is None or stored_step["receipt_digest"] != receipt_digest:
                raise ValueError("Crescendo step receipt conflicts with acknowledged effect")
            step_digests.append(str(stored_step["receipt_digest"]))
            stage = "backtrack" if decision == "refusal" else "escalate"
        if terminal is None:
            node = self.connection.execute(
                """SELECT state FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = %s""",
                (schedule_id, f"worker:{assignment_id}"),
            ).fetchone()
            terminal = (
                "cancelled"
                if node is not None and node["state"] == "cancelled"
                else "ambiguous"
                if node is not None and node["state"] in {"failed", "quarantined"}
                else None
            )
            if terminal is None:
                return
        verdict = "ambiguous"
        terminal_value = {
            "schedule_id": schedule_id,
            "assignment_id": assignment_id,
            "semantic_technique_id": contract["semantic_technique_id"],
            "adapter_digest": contract["reviewed_adapter_digest"],
            "contract_digest": _qualified_digest(contract),
            "target_session_id": target_session,
            "terminal_reason": terminal,
            "verdict": verdict,
            "step_receipt_digests": step_digests,
        }
        self.connection.execute(
            """INSERT INTO merlin_storage.crescendo_terminal_receipts
               (schedule_id, assignment_id, contract_digest, target_session_id,
                semantic_technique_id, adapter_digest, terminal_reason, verdict,
                step_receipt_digests, receipt_digest)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                schedule_id,
                assignment_id,
                terminal_value["contract_digest"],
                target_session,
                contract["semantic_technique_id"],
                contract["reviewed_adapter_digest"],
                terminal,
                verdict,
                Jsonb(step_digests),
                _qualified_digest(terminal_value),
            ),
        )
        stored_terminal = self.connection.execute(
            """SELECT receipt_digest FROM merlin_storage.crescendo_terminal_receipts
               WHERE schedule_id = %s AND assignment_id = %s""",
            (schedule_id, assignment_id),
        ).fetchone()
        if stored_terminal is None or stored_terminal["receipt_digest"] != _qualified_digest(
            terminal_value
        ):
            raise ValueError("Crescendo terminal receipt conflicts with effect lineage")

    def _reconcile_baseline_comparisons(self, schedule_id: str) -> None:
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(schedule_id)
        assignments = tuple(
            item
            for item in schedule["graph"].get("assignments", ())
            if isinstance(item, Mapping) and item.get("agenda_id")
        )
        agendas = sorted({str(item["agenda_id"]) for item in assignments})
        for agenda_id in agendas:
            agenda_assignments = tuple(
                item for item in assignments if str(item["agenda_id"]) == agenda_id
            )
            expected_stages = sorted(
                (
                    str(item["execution_stage"])
                    for item in agenda_assignments
                    if item.get("execution_stage") in {"control", "direct", "replay"}
                ),
                key={"control": 0, "direct": 1, "replay": 2}.__getitem__,
            )
            stages = self.connection.execute(
                """SELECT * FROM merlin_storage.baseline_technique_attempts
                   WHERE schedule_id = %s AND agenda_id = %s
                   ORDER BY CASE stage WHEN 'control' THEN 1 WHEN 'direct' THEN 2 ELSE 3 END""",
                (schedule_id, agenda_id),
            ).fetchall()
            stages_complete = [row["stage"] for row in stages] == expected_stages and all(
                row["outcome"] == "completed" for row in stages
            )
            if not stages_complete:
                # Missing execution is not an evaluation result. Keep this agenda
                # unresolved so discovery, repair, or a later worker can attempt it.
                continue
            receipt = self._evaluate_baseline_comparison(agenda_assignments, stages)
            values = (
                schedule_id,
                agenda_id,
                receipt["pair_id"],
                receipt["policy"],
                receipt["policy_digest"],
                receipt["oracle_binding_digest"],
                receipt["adapter_digest"],
                Jsonb(receipt["stage_receipts"]),
                Jsonb(receipt["transform_artifacts"]),
                receipt["verdict"],
                receipt["reason"],
                receipt["receipt_digest"],
            )
            inserted = self.connection.execute(
                """INSERT INTO merlin_storage.baseline_comparison_receipts
                       (schedule_id, agenda_id, pair_id, policy, policy_digest,
                        oracle_binding_digest, adapter_digest, stage_receipts,
                        transform_artifacts, verdict, reason, receipt_digest)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING id""",
                values,
            ).fetchone()
            if inserted is None:
                existing = self.connection.execute(
                    """SELECT pair_id, policy, policy_digest, oracle_binding_digest,
                              adapter_digest, stage_receipts, transform_artifacts, verdict,
                              reason, receipt_digest
                       FROM merlin_storage.baseline_comparison_receipts
                       WHERE schedule_id = %s AND agenda_id = %s""",
                    (schedule_id, agenda_id),
                ).fetchone()
                if existing is None:
                    # The receipt digest covers content only, not schedule/agenda
                    # identity, so an identical receipt (a capability-blocked pair
                    # repeated across agendas, or a replay under a new schedule)
                    # dedupes to the canonical row. Accept the dedupe only when
                    # the stored row seals exactly this receipt.
                    existing = self.connection.execute(
                        """SELECT pair_id, policy, policy_digest, oracle_binding_digest,
                                  adapter_digest, stage_receipts, transform_artifacts, verdict,
                                  reason, receipt_digest
                           FROM merlin_storage.baseline_comparison_receipts
                           WHERE receipt_digest = %s""",
                        (receipt["receipt_digest"],),
                    ).fetchone()
                existing_unsigned = (
                    {}
                    if existing is None
                    else {
                        key: existing[key]
                        for key in (
                            "pair_id",
                            "policy",
                            "policy_digest",
                            "oracle_binding_digest",
                            "adapter_digest",
                            "stage_receipts",
                            "transform_artifacts",
                            "verdict",
                            "reason",
                        )
                    }
                )
                if (
                    existing is None
                    or existing["receipt_digest"] != _qualified_digest(existing_unsigned)
                    or existing["receipt_digest"] != receipt["receipt_digest"]
                ):
                    raise ValueError("terminal baseline comparison receipt is immutable")

    def _evaluate_baseline_comparison(
        self,
        assignments: Sequence[Mapping[str, object]],
        stages: Sequence[Any],
        schedule_id: str | None = None,
    ) -> dict[str, object]:
        schedule_id = str(stages[0]["schedule_id"]) if stages else (schedule_id or "")
        campaign_owner = self._baseline_campaign_owner(schedule_id)
        by_stage = {str(item["execution_stage"]): item for item in assignments}
        pair_ids = {str(item.get("pair_id")) for item in assignments}
        policies = {str(item.get("comparison_policy")) for item in assignments}
        pair_id = next(iter(pair_ids)) if len(pair_ids) == 1 else "mismatched-pair"
        policy = next(iter(policies)) if len(policies) == 1 else "mismatched-policy"
        policy_digest = _qualified_digest(
            {"evaluator": "merlin.api-json-pointer.v1", "policy": policy}
        )
        stage_receipts: list[dict[str, object]] = []
        observations: list[Mapping[str, object]] = []
        reason: str | None = None
        for stage in stages:
            effect = self.connection.execute(
                """SELECT idempotency_key, attempt_id::text, campaign_id, target_id, tool_name,
                          call_id,
                          arguments_digest, result_metadata
                   FROM merlin_storage.tool_effects WHERE idempotency_key = %s""",
                (stage["target_effect_id"],),
            ).fetchone()
            metadata = {} if effect is None else effect["result_metadata"] or {}
            exchanges = metadata.get("target_exchanges", ())
            interactions = (
                tuple(
                    item
                    for item in exchanges
                    if isinstance(item, Mapping)
                    and item.get("interaction") is True
                    and item.get("cleanup") is not True
                )
                if isinstance(exchanges, list)
                else ()
            )
            observation = (
                interactions[0].get("oracle_observation") if len(interactions) == 1 else None
            )
            if effect is None or effect["tool_name"] != "target_request":
                reason = reason or "no reviewed deterministic observation extractor for modality"
            elif len(interactions) != 1 or not isinstance(observation, Mapping):
                reason = reason or "exact API oracle observation is missing or ambiguous"
            else:
                observations.append(observation)
            assignment = by_stage.get(str(stage["stage"]), {})
            effect_target_session_id = metadata.get(
                "remote_target_session_id", metadata.get("target_session_id")
            )
            if effect is not None and (
                str(effect["attempt_id"]) != str(stage["scheduler_attempt_id"])
                or effect["target_id"] != assignment.get("target_profile_id")
                or effect_target_session_id != stage["target_session_id"]
                or (
                    campaign_owner is not None
                    and (
                        effect["campaign_id"] != campaign_owner
                        or metadata.get("campaign_id") != campaign_owner
                    )
                )
            ):
                reason = reason or (
                    "effect campaign ownership drifted"
                    if campaign_owner is not None
                    and (
                        effect["campaign_id"] != campaign_owner
                        or metadata.get("campaign_id") != campaign_owner
                    )
                    else "effect ownership or reviewed target session drifted"
                )
            stage_receipts.append(
                {
                    "stage": stage["stage"],
                    "stage_receipt_id": str(stage["id"]),
                    "attempt_id": str(stage["scheduler_attempt_id"]),
                    "result_digest": stage["result_digest"],
                    "effect_id": stage["target_effect_id"],
                    "effect_arguments_digest": (
                        None if effect is None else effect["arguments_digest"]
                    ),
                    "exchange_digest": (
                        _qualified_digest(interactions[0]) if len(interactions) == 1 else None
                    ),
                    "exchange_id": (
                        f"{effect['call_id']}:{interactions[0].get('id', 0)}"
                        if effect is not None and len(interactions) == 1
                        else None
                    ),
                    "oracle_observation_digest": (
                        observation.get("observation_digest")
                        if isinstance(observation, Mapping)
                        else None
                    ),
                    "oracle_observation": (
                        dict(observation) if isinstance(observation, Mapping) else None
                    ),
                    "measurements": _exchange_measurements(interactions),
                    "transform_bindings": assignment.get("transform_bindings", ()),
                }
            )
        verdict = "ambiguous"
        binding_digest = adapter_digest = None
        if len(pair_ids) != 1 or len(policies) != 1:
            reason = "pair or reviewed comparison policy differs across stages"
        elif policy != "reviewed-benign-differential-v1":
            reason = "reviewed comparison policy has no deterministic evaluator"
        elif "replay" in by_stage and by_stage.get("direct", {}).get(
            "transform_bindings", ()
        ) != by_stage["replay"].get("transform_bindings", ()):
            reason = "direct/replay transform artifact binding differs"
        elif reason is None and len(observations) == len(stages) and len(stages) in {2, 3}:
            if not all(_valid_oracle_observation(item) for item in observations):
                reason = "API oracle observation integrity check failed"
            else:
                binding_values = {str(item["binding_digest"]) for item in observations}
                adapter_values = {str(item["adapter_digest"]) for item in observations}
                reviewed_adapter_values = {
                    str(item.get("reviewed_adapter_digest")) for item in assignments
                }
                oracle_values = {str(item["oracle_digest"]) for item in observations}
                expected_dimensions = {
                    tuple(tuple(part) for part in item.get("comparison_dimensions", ()))
                    for item in assignments
                }
                observed_dimensions = {
                    (
                        ("modality", str(item["modality"])),
                        ("turn", str(item["turn"])),
                        ("language", str(item["language"])),
                    )
                    for item in observations
                }
                expected_dimension = (
                    dict(next(iter(expected_dimensions))) if len(expected_dimensions) == 1 else {}
                )
                observed_dimension = (
                    dict(next(iter(observed_dimensions))) if len(observed_dimensions) == 1 else {}
                )
                # The observation describes the reviewed oracle adapter's
                # capability (for example an English, multi-turn API), not the
                # language/turn shape of this individual delivered prompt.
                # Request-language truth is sealed separately by the target
                # delivery receipt.  Only a real modality mismatch makes this
                # oracle incapable of evaluating the admitted cell.
                dimensions_compatible = (
                    len(expected_dimensions) == 1
                    and len(observed_dimensions) == 1
                    and expected_dimension.get("modality") == observed_dimension.get("modality")
                )
                if (
                    len(binding_values) != 1
                    or len(adapter_values) != 1
                    or adapter_values != reviewed_adapter_values
                    or len(oracle_values) != 1
                    or not dimensions_compatible
                ):
                    reason = "adapter, oracle, or semantic dimensions drifted across stages"
                else:
                    binding_digest = next(iter(binding_values))
                    adapter_digest = next(iter(adapter_values))
                    reason = "deterministic observations recorded; validator review is required"
        transform_artifacts = [
            {"stage": item["stage"], "bindings": item["transform_bindings"]}
            for item in stage_receipts
        ]
        unsigned = {
            "pair_id": pair_id,
            "policy": policy,
            "policy_digest": policy_digest,
            "oracle_binding_digest": binding_digest,
            "adapter_digest": adapter_digest,
            "stage_receipts": stage_receipts,
            "transform_artifacts": transform_artifacts,
            "verdict": verdict,
            "reason": reason,
        }
        return {
            **unsigned,
            "receipt_digest": _qualified_digest(unsigned),
            "permanent": False,
        }

    def _baseline_campaign_owner(self, schedule_id: str) -> str | None:
        rows = self.connection.execute(
            """SELECT DISTINCT campaign_id FROM merlin_storage.campaign_waves
               WHERE schedule_id = %s""",
            (schedule_id,),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("baseline schedule belongs to multiple campaigns")
        return None if not rows else str(rows[0]["campaign_id"])

    @_serialized
    def validation_is_independent(
        self, source_schedule_id: str, validation_schedule_id: str
    ) -> bool:
        def completed_bindings(
            schedule_id: str, *, validator: bool
        ) -> set[tuple[str, str, str, str]]:
            schedule = self.connection.execute(
                "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
                (schedule_id,),
            ).fetchone()
            if schedule is None:
                return set()
            assignments = {
                str(item.get("id")): item
                for item in schedule["graph"].get("assignments", ())
                if isinstance(item, Mapping)
            }
            rows = self.connection.execute(
                """SELECT DISTINCT attempt.node_id, attempt.target_session_id
                   FROM merlin_storage.scheduler_attempts attempt
                   JOIN merlin_storage.scheduler_results result
                     ON result.attempt_id = attempt.id
                   WHERE attempt.schedule_id = %s AND attempt.state = 'completed'
                     AND result.node_id LIKE %s""",
                (
                    schedule_id,
                    "worker:%/independent-validator" if validator else "worker:%",
                ),
            ).fetchall()
            bindings: set[tuple[str, str, str, str]] = set()
            for row in rows:
                if row["target_session_id"] is None:
                    continue
                assignment = assignments.get(str(row["node_id"]).removeprefix("worker:"), {})
                contract = dict(assignment.get("strategy_contract") or ())
                session_id = row["target_session_id"]
                target_id = assignment.get("target_profile_id")
                adapter = contract.get("reviewed_adapter_digest")
                equivalence = contract.get("initial_state_equivalence_digest")
                if not all(
                    isinstance(value, str) and value
                    for value in (session_id, target_id, adapter, equivalence)
                ):
                    return set()
                bindings.add((str(session_id), str(target_id), str(adapter), str(equivalence)))
            return bindings

        source_bindings = completed_bindings(source_schedule_id, validator=False)
        validation_bindings = completed_bindings(validation_schedule_id, validator=True)
        source_states = {binding[1:] for binding in source_bindings}
        validation_states = {binding[1:] for binding in validation_bindings}
        source_sessions = {binding[0] for binding in source_bindings}
        validation_sessions = {binding[0] for binding in validation_bindings}
        return (
            len(source_states) == 1
            and source_states == validation_states
            and bool(source_sessions)
            and bool(validation_sessions)
            and source_sessions.isdisjoint(validation_sessions)
        )

    @_serialized
    def admit_target_adapter_probe(
        self, scope_key: str, *, probe_lease_seconds: int = 30
    ) -> str | None:
        """Admit one worker, or exactly one half-open recovery probe after cooldown."""
        if not scope_key.strip() or probe_lease_seconds < 1:
            raise ValueError("invalid target adapter circuit probe lease")
        with self.connection.transaction():
            row = self.connection.execute(
                """SELECT state, retry_at <= now() AS retry_ready
                   FROM merlin_storage.target_adapter_circuits
                   WHERE scope_key = %s FOR UPDATE""",
                (scope_key,),
            ).fetchone()
            if row is None:
                return "closed"
            if not row["retry_ready"]:
                return None
            token = uuid4()
            self.connection.execute(
                """UPDATE merlin_storage.target_adapter_circuits
                   SET state = 'half_open',
                       retry_at = now() + make_interval(secs => %s),
                       probe_token = %s,
                       updated_at = now()
                   WHERE scope_key = %s""",
                (probe_lease_seconds, token, scope_key),
            )
            return f"half_open:{token}"

    @_serialized
    def renew_target_adapter_probe(
        self, scope_key: str, probe_token: str, *, probe_lease_seconds: int = 30
    ) -> bool:
        """Renew only the currently fenced half-open probe lease."""
        if not scope_key.strip() or not probe_token.strip() or probe_lease_seconds < 1:
            raise ValueError("invalid target adapter circuit probe lease")
        cursor = self.connection.execute(
            """UPDATE merlin_storage.target_adapter_circuits
               SET retry_at = now() + make_interval(secs => %s), updated_at = now()
               WHERE scope_key = %s AND state = 'half_open' AND probe_token = %s
                 AND retry_at > now()""",
            (probe_lease_seconds, scope_key, probe_token),
        )
        return cursor.rowcount == 1

    @_serialized
    def release_target_adapter_probe(self, scope_key: str, probe_token: str) -> bool:
        """Abandon an unexpired half-open probe without counting an adapter failure."""
        if not scope_key.strip() or not probe_token.strip():
            raise ValueError("invalid target adapter circuit probe release")
        cursor = self.connection.execute(
            """UPDATE merlin_storage.target_adapter_circuits
               SET state = 'open', probe_token = NULL, retry_at = now(), updated_at = now()
               WHERE scope_key = %s AND state = 'half_open' AND probe_token = %s
                 AND retry_at > now()""",
            (scope_key, probe_token),
        )
        return cursor.rowcount == 1

    @_serialized
    def target_adapter_retry_at(self, scope_key: str) -> datetime | None:
        """Return the durable wake time for work waiting behind this circuit."""
        if not scope_key.strip():
            raise ValueError("target adapter circuit scope must not be blank")
        row = self.connection.execute(
            """SELECT retry_at FROM merlin_storage.target_adapter_circuits
               WHERE scope_key = %s""",
            (scope_key,),
        ).fetchone()
        return None if row is None else row["retry_at"]

    @_serialized
    def record_target_adapter_failure(
        self,
        scope_key: str,
        *,
        retry_after_seconds: int = 30,
        probe_token: str | None = None,
    ) -> None:
        """Open the shared circuit after a worker proves the delivery path unhealthy."""
        if not scope_key.strip() or retry_after_seconds < 0:
            raise ValueError("invalid target adapter circuit failure")
        if probe_token is not None:
            self.connection.execute(
                """UPDATE merlin_storage.target_adapter_circuits
                   SET state = 'open', probe_token = NULL,
                       failure_count = failure_count + 1,
                       retry_at = now() + make_interval(secs => %s), updated_at = now()
                   WHERE scope_key = %s AND state = 'half_open' AND probe_token = %s
                     AND retry_at > now()""",
                (retry_after_seconds, scope_key, probe_token),
            )
            return
        self.connection.execute(
            """INSERT INTO merlin_storage.target_adapter_circuits
                   (scope_key, state, failure_count, retry_at)
               VALUES (%s, 'open', 1, now() + make_interval(secs => %s))
               ON CONFLICT (scope_key) DO UPDATE
               SET state = 'open',
                   probe_token = NULL,
                   failure_count = merlin_storage.target_adapter_circuits.failure_count + 1,
                   retry_at = excluded.retry_at,
                   updated_at = now()""",
            (scope_key, retry_after_seconds),
        )

    @_serialized
    def record_target_adapter_success(
        self, scope_key: str, *, probe_token: str | None = None
    ) -> None:
        """Close a half-open circuit only after its admitted probe succeeds."""
        if not scope_key.strip():
            raise ValueError("target adapter circuit scope must not be blank")
        if probe_token is None:
            return
        self.connection.execute(
            """DELETE FROM merlin_storage.target_adapter_circuits
               WHERE scope_key = %s AND state = 'half_open' AND probe_token = %s
                 AND retry_at > now()""",
            (scope_key, probe_token),
        )

    @_serialized
    def verify_committed_evidence(self, evidence: object) -> bool:
        """Verify exact attributable evidence against an atomically committed result row."""
        schedule_id = getattr(evidence, "schedule_id", None)
        node_id = getattr(evidence, "node_id", None)
        attempt_id = getattr(evidence, "attempt_id", None)
        result_digest = getattr(evidence, "result_digest", None)
        if not all(
            isinstance(value, str) and value
            for value in (schedule_id, node_id, attempt_id, result_digest)
        ):
            return False
        if result_digest.startswith("sha256:"):
            result_digest = result_digest.removeprefix("sha256:")
        row = self.connection.execute(
            """SELECT 1 FROM merlin_storage.scheduler_results r
               JOIN merlin_storage.scheduler_attempts a ON a.id = r.attempt_id
               WHERE r.schedule_id = %s AND r.node_id = %s AND r.attempt_id = %s
                 AND r.payload_digest = %s AND a.state = 'completed'""",
            (schedule_id, node_id, attempt_id, result_digest),
        ).fetchone()
        return row is not None

    @_serialized
    def restore(self, schedule_id: str) -> dict[str, Any]:
        """Validate and return the complete durable projection needed after restart."""
        self.verify(schedule_id)
        schedule = self.load_schedule(schedule_id)
        if schedule is None:
            raise KeyError(f"unknown schedule: {schedule_id}")
        return {
            "schedule": schedule,
            "nodes": self.list_nodes(schedule_id),
            "attempts": self.list_attempts(schedule_id),
            "results": self.list_results(schedule_id),
            "mailbox": self.list_mailbox(schedule_id),
            "completions": self.list_completions(schedule_id),
            "events": self.events(schedule_id),
        }

    @_serialized
    def list_mailbox(self, schedule_id: str) -> list[dict[str, Any]]:
        """Return durable delivery state, including claims awaiting acknowledgement."""
        rows = self.connection.execute(
            """SELECT id, schedule_id, sender, recipient, schema_id, payload,
                      payload_digest, claimed_by_attempt, claimed_generation,
                      created_at, acknowledged_at
               FROM merlin_storage.scheduler_mailbox
               WHERE schedule_id = %s ORDER BY created_at, id""",
            (schedule_id,),
        ).fetchall()
        return [
            {
                **row,
                "id": str(row["id"]),
                "schedule_id": str(row["schedule_id"]),
                "claimed_by_attempt": (
                    str(row["claimed_by_attempt"])
                    if row["claimed_by_attempt"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    @_serialized
    def list_completions(
        self, schedule_id: str, *, unacknowledged_only: bool = False
    ) -> list[dict[str, Any]]:
        """Project terminal child completions for restart-safe fan-in delivery."""
        rows = self.connection.execute(
            """SELECT attempt_id, schedule_id, node_id, result_digest,
                      published_at, acknowledged_at
               FROM merlin_storage.scheduler_completions
               WHERE schedule_id = %s
                 AND (%s = false OR acknowledged_at IS NULL)
               ORDER BY published_at, attempt_id""",
            (schedule_id, unacknowledged_only),
        ).fetchall()
        return [
            {
                **row,
                "attempt_id": str(row["attempt_id"]),
                "schedule_id": str(row["schedule_id"]),
            }
            for row in rows
        ]

    @_serialized
    def transition_ready(
        self,
        schedule_id: str,
        node_ids: Sequence[str],
        *,
        checkpoint: dict[str, object],
        expected_event_count: int,
        event_type: str = "nodes_ready",
        payload: dict[str, object] | None = None,
    ) -> None:
        """Atomically expose pending nodes and advance the checkpoint/event head."""
        if not node_ids:
            raise ValueError("at least one node must become ready")
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT event_count FROM merlin_storage.scheduler_schedules
                   WHERE id = %s AND status = 'running' FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None or schedule["event_count"] != expected_event_count:
                raise StaleLeaseError("scheduler checkpoint base changed")
            requested = sorted(set(node_ids))
            known = {
                str(row["node_id"])
                for row in self.connection.execute(
                    """SELECT node_id FROM merlin_storage.scheduler_nodes
                       WHERE schedule_id = %s AND node_id = ANY(%s)""",
                    (schedule_id, requested),
                ).fetchall()
            }
            if any(node_id not in known for node_id in requested):
                raise ValueError("one or more nodes are unknown")
            ready_ids = [
                str(row["node_id"])
                for row in self.connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes SET state = 'ready'
                       WHERE schedule_id = %s AND node_id = ANY(%s) AND state = 'pending'
                       RETURNING node_id""",
                    (schedule_id, requested),
                ).fetchall()
            ]
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                event_type,
                payload
                or {
                    "node_ids": sorted(ready_ids),
                    "checkpoint_digest": checkpoint_digest,
                },
            )

    @_serialized
    def transition_failed(
        self,
        schedule_id: str,
        failures: Mapping[str, str],
        *,
        checkpoint: dict[str, object],
        expected_event_count: int,
    ) -> None:
        """Atomically terminalize pending nodes whose prerequisites cannot succeed."""
        if not failures:
            raise ValueError("at least one pending node must fail")
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT event_count FROM merlin_storage.scheduler_schedules
                   WHERE id = %s AND status = 'running' FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None or schedule["event_count"] != expected_event_count:
                raise StaleLeaseError("scheduler checkpoint base changed")
            failed_ids: list[str] = []
            for node_id in sorted(failures):
                known = self.connection.execute(
                    """SELECT 1 FROM merlin_storage.scheduler_nodes
                       WHERE schedule_id = %s AND node_id = %s""",
                    (schedule_id, node_id),
                ).fetchone()
                if known is None:
                    raise ValueError("one or more nodes are unknown")
                transitioned = self.connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes SET state = 'failed'
                       WHERE schedule_id = %s AND node_id = %s AND state = 'pending'
                       RETURNING node_id""",
                    (schedule_id, node_id),
                ).fetchone()
                if transitioned is not None:
                    failed_ids.append(node_id)
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                "prerequisites_failed",
                {
                    "failures": {node_id: failures[node_id] for node_id in sorted(failed_ids)},
                    "checkpoint_digest": checkpoint_digest,
                },
            )

    @_serialized
    def claim(
        self,
        schedule_id: str,
        node_id: str,
        owner: str,
        *,
        lease_expires_at: datetime,
        attempt_id: str | None = None,
    ) -> AttemptLease:
        if lease_expires_at <= datetime.now(lease_expires_at.tzinfo):
            raise ValueError("lease expiry must be in the future")
        identifier = attempt_id or str(uuid4())
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            cleanup_after_cancel = (
                schedule is not None
                and schedule["status"] == "cancelled"
                and node_id.startswith("cleanup:")
            )
            if schedule is None or (schedule["status"] != "running" and not cleanup_after_cancel):
                raise StaleLeaseError("scheduler schedule is not running")
            if (
                not node_id.startswith("cleanup:")
                and self.connection.execute(
                    """SELECT 1 FROM merlin_storage.scheduler_events
                   WHERE schedule_id = %s AND type = 'schedule_draining' LIMIT 1""",
                    (schedule_id,),
                ).fetchone()
                is not None
            ):
                raise StaleLeaseError("scheduler is finishing current work")
            node = self.connection.execute(
                """SELECT state, lease_generation FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = %s FOR UPDATE""",
                (schedule_id, node_id),
            ).fetchone()
            if node is None or node["state"] != "ready":
                raise StaleLeaseError("scheduler node is not ready")
            generation = node["lease_generation"] + 1
            replaced = self.connection.execute(
                """SELECT id FROM merlin_storage.scheduler_attempts
                   WHERE schedule_id = %s AND node_id = %s
                     AND state IN ('failed', 'expired')
                   ORDER BY generation DESC LIMIT 1""",
                (schedule_id, node_id),
            ).fetchone()
            replaced_attempt_id = replaced["id"] if replaced is not None else None
            row = self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_attempts
                       (id, schedule_id, node_id, generation, owner, state,
                        lease_expires_at, heartbeat_at, replaced_attempt_id)
                   VALUES (%s, %s, %s, %s, %s, 'running', %s, now(), %s)
                   RETURNING id, schedule_id, node_id, generation, owner, lease_expires_at""",
                (
                    identifier,
                    schedule_id,
                    node_id,
                    generation,
                    owner,
                    lease_expires_at,
                    replaced_attempt_id,
                ),
            ).fetchone()
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = 'leased', lease_generation = %s,
                       attempts_started = attempts_started + 1
                   WHERE schedule_id = %s AND node_id = %s""",
                (generation, schedule_id, node_id),
            )
            self._append_event(
                schedule_id,
                "claimed",
                {
                    "node_id": node_id,
                    "attempt_id": identifier,
                    "generation": generation,
                    "replaced_attempt_id": str(replaced_attempt_id)
                    if replaced_attempt_id is not None
                    else None,
                },
            )
        return AttemptLease(**{**row, "id": str(row["id"]), "schedule_id": str(row["schedule_id"])})

    @_serialized
    def pause_schedule(self, schedule_id: str, *, reason: str) -> None:
        """Fence live attempts and make their nodes immediately resumable."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET status = 'paused', updated_at = now()
                   WHERE id = %s AND status = 'running' RETURNING id""",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise ValueError("schedule is unknown or not running")
            fenced = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = 'expired', error = %s, completed_at = now()
                   WHERE schedule_id = %s AND state = 'running'
                   RETURNING id, node_id, generation""",
                (f"clean pause: {reason}", schedule_id),
            ).fetchall()
            for attempt in fenced:
                self.connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes
                       SET state = 'ready'
                       WHERE schedule_id = %s AND node_id = %s
                         AND state = 'leased' AND lease_generation = %s""",
                    (schedule_id, attempt["node_id"], attempt["generation"]),
                )
                self._release_message_claims(str(attempt["id"]), int(attempt["generation"]))
            self._append_event(
                schedule_id,
                "schedule_paused",
                {
                    "reason": reason,
                    "fenced_attempt_ids": [str(attempt["id"]) for attempt in fenced],
                },
            )

    @_serialized
    def resume_schedule(self, schedule_id: str) -> None:
        """Re-admit claims after an operator-paused durable schedule."""
        with self.connection.transaction():
            row = self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET status = 'running', updated_at = now()
                   WHERE id = %s AND status = 'paused' RETURNING id""",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise ValueError("schedule is unknown or not paused")
            self._append_event(schedule_id, "schedule_resumed", {})

    @_serialized
    def heartbeat(
        self, attempt_id: str, generation: int, *, lease_expires_at: datetime
    ) -> AttemptLease:
        if lease_expires_at <= datetime.now(lease_expires_at.tzinfo):
            raise ValueError("lease expiry must be in the future")
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation)
            row = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET heartbeat_at = now(), lease_expires_at = %s
                   WHERE id = %s RETURNING id, schedule_id, node_id, generation, owner,
                                               lease_expires_at""",
                (lease_expires_at, attempt_id),
            ).fetchone()
            self._append_event(
                str(attempt["schedule_id"]),
                "heartbeat",
                {"attempt_id": attempt_id, "generation": generation},
            )
        return AttemptLease(**{**row, "id": str(row["id"]), "schedule_id": str(row["schedule_id"])})

    @_serialized
    def expire(self, attempt_id: str, generation: int) -> None:
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation, require_unexpired=False)
            if attempt["lease_expires_at"] > datetime.now(attempt["lease_expires_at"].tzinfo):
                raise StaleLeaseError("attempt lease has not expired")
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = 'expired', completed_at = now() WHERE id = %s""",
                (attempt_id,),
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes SET state = 'ready'
                   WHERE schedule_id = %s AND node_id = %s AND lease_generation = %s""",
                (attempt["schedule_id"], attempt["node_id"], generation),
            )
            self._release_message_claims(attempt_id, generation)
            self._append_event(
                str(attempt["schedule_id"]),
                "expired",
                {"attempt_id": attempt_id, "generation": generation},
            )

    @_serialized
    def find_overdue_attempts(self, *, at: datetime, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id, schedule_id, node_id, generation, owner, lease_expires_at
               FROM merlin_storage.scheduler_attempts
               WHERE state = 'running' AND lease_expires_at <= %s
               ORDER BY lease_expires_at, id LIMIT %s""",
            (at, limit),
        ).fetchall()
        return [
            {**row, "id": str(row["id"]), "schedule_id": str(row["schedule_id"])} for row in rows
        ]

    @_serialized
    def expire_overdue(self, *, at: datetime, limit: int = 100) -> list[str]:
        expired: list[str] = []
        for attempt in self.find_overdue_attempts(at=at, limit=limit):
            self.expire(attempt["id"], attempt["generation"])
            expired.append(attempt["id"])
        return expired

    @_serialized
    def append_schedule_event(
        self, schedule_id: str, event_type: str, payload: dict[str, object]
    ) -> None:
        """Advisory executor-lifecycle journaling; never fencing-authoritative."""
        with self.connection.transaction():
            self._append_event(schedule_id, event_type, payload)

    @_serialized
    def settle_attempt(
        self,
        attempt_id: str,
        generation: int,
        outcome: str,
        *,
        reason: str,
        checkpoint: dict[str, object],
        target_session_id: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        if outcome not in {"failed", "cancelled", "quarantined"}:
            raise ValueError("attempt outcome must be failed, cancelled, or quarantined")
        if failure_kind is not None and failure_kind not in _ATTEMPT_FAILURE_KINDS:
            raise ValueError("attempt failure kind is unknown")
        target_session_id = _target_session_id(target_session_id)
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            cancelled = self.connection.execute(
                """SELECT attempt.schedule_id, attempt.node_id
                   FROM merlin_storage.scheduler_attempts AS attempt
                   JOIN merlin_storage.scheduler_nodes AS node
                     ON node.schedule_id = attempt.schedule_id
                    AND node.node_id = attempt.node_id
                   WHERE attempt.id = %s AND attempt.generation = %s
                     AND attempt.state = 'cancelled' AND node.state = 'cancelled'
                     AND node.lease_generation = attempt.generation""",
                (attempt_id, generation),
            ).fetchone()
            if cancelled is not None:
                # Durable cancellation is authoritative. A provider may finish while
                # cancellation propagates; discard every late terminal result.
                return
            attempt = self._lock_attempt(attempt_id, generation)
            schedule_id = str(attempt["schedule_id"])
            updated = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = %s, error = %s, completed_at = now(),
                       target_session_id = coalesce(target_session_id, %s),
                       failure_kind = %s
                   WHERE id = %s
                     AND (%s::text IS NULL OR target_session_id IS NULL
                          OR target_session_id = %s)""",
                (
                    outcome,
                    reason,
                    target_session_id,
                    failure_kind,
                    attempt_id,
                    target_session_id,
                    target_session_id,
                ),
            )
            if updated.rowcount != 1:
                raise StaleLeaseError("attempt target session identity changed")
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = %s, failed_attempts = failed_attempts + %s
                   WHERE schedule_id = %s AND node_id = %s AND lease_generation = %s""",
                (
                    outcome,
                    1 if outcome == "failed" else 0,
                    schedule_id,
                    attempt["node_id"],
                    generation,
                ),
            )
            self._release_message_claims(attempt_id, generation)
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                outcome,
                {
                    "node_id": attempt["node_id"],
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "reason": reason,
                    "failure_kind": failure_kind,
                    "checkpoint_digest": checkpoint_digest,
                },
            )

    @_serialized
    def retry_failed(
        self,
        schedule_id: str,
        node_id: str,
        *,
        checkpoint: dict[str, object],
        expected_event_count: int,
    ) -> None:
        """Requeue one failed or infrastructure-quarantined node preserving history."""
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT event_count FROM merlin_storage.scheduler_schedules
                   WHERE id = %s AND status = 'running' FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None or schedule["event_count"] != expected_event_count:
                raise StaleLeaseError("scheduler checkpoint base changed")
            cursor = self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes SET state = 'ready'
                   WHERE schedule_id = %s AND node_id = %s
                     AND state IN ('failed', 'quarantined')""",
                (schedule_id, node_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("scheduler node is unknown or not retryable")
            checkpoint_nodes = checkpoint.get("nodes")
            retry_state = (
                dict(checkpoint_nodes.get(node_id) or {})
                if isinstance(checkpoint_nodes, Mapping)
                else {}
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                "retry_scheduled",
                {
                    "node_id": node_id,
                    "checkpoint_digest": checkpoint_digest,
                    "retry_state": retry_state,
                },
            )

    @_serialized
    def cancel_schedule(
        self,
        schedule_id: str,
        *,
        reason: str,
        cleanup_node_ids: Sequence[str],
        checkpoint: dict[str, object],
    ) -> None:
        """Cancel ordinary work while preserving cleanup nodes as dispatchable."""
        cleanup = list(set(cleanup_node_ids))
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None or schedule["status"] not in {"running", "paused"}:
                raise ValueError("schedule is unknown or terminal")
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts AS attempt
                   SET state = 'cancelled', error = %s, completed_at = now()
                   FROM merlin_storage.scheduler_nodes AS node
                   WHERE attempt.schedule_id = %s AND attempt.state = 'running'
                     AND node.schedule_id = attempt.schedule_id AND node.node_id = attempt.node_id
                     AND NOT (node.node_id = ANY(%s))""",
                (reason, schedule_id, cleanup),
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes SET state = 'cancelled'
                   WHERE schedule_id = %s AND state IN ('pending', 'ready', 'leased')
                     AND NOT (node_id = ANY(%s))""",
                (schedule_id, cleanup),
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_mailbox AS message
                   SET claimed_by_attempt = NULL, claimed_generation = NULL
                   FROM merlin_storage.scheduler_attempts AS attempt
                   WHERE message.claimed_by_attempt = attempt.id
                     AND message.claimed_generation = attempt.generation
                     AND attempt.schedule_id = %s AND attempt.state = 'cancelled'
                     AND message.acknowledged_at IS NULL""",
                (schedule_id,),
            )
            if cleanup:
                cleanup_rows = self.connection.execute(
                    """SELECT node_id, state FROM merlin_storage.scheduler_nodes
                       WHERE schedule_id = %s AND node_id = ANY(%s) FOR UPDATE""",
                    (schedule_id, cleanup),
                ).fetchall()
                cleanup_states = {str(row["node_id"]): str(row["state"]) for row in cleanup_rows}
                if set(cleanup_states) != set(cleanup) or any(
                    state not in {"pending", "ready", "completed"}
                    for state in cleanup_states.values()
                ):
                    raise ValueError("cleanup nodes must exist and not have active or failed work")
                self.connection.execute(
                    """UPDATE merlin_storage.scheduler_nodes SET state = 'ready'
                       WHERE schedule_id = %s AND node_id = ANY(%s)
                         AND state IN ('pending', 'ready')""",
                    (schedule_id, cleanup),
                )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET status = 'cancelled', checkpoint = %s, checkpoint_digest = %s,
                       updated_at = now() WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                "schedule_cancelled",
                {"reason": reason, "cleanup_node_ids": sorted(cleanup)},
            )

    @_serialized
    def drain_schedule(self, schedule_id: str, *, reason: str) -> tuple[str, ...]:
        """Atomically fence new ordinary claims, preserving owned attempts and cleanup."""
        with self.connection.transaction():
            row = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown scheduler schedule: {schedule_id}")
            if row["status"] != "running":
                return ()
            existing = self.connection.execute(
                """SELECT 1 FROM merlin_storage.scheduler_events
                   WHERE schedule_id = %s AND type = 'schedule_draining' LIMIT 1""",
                (schedule_id,),
            ).fetchone()
            if existing is None:
                self._append_event(schedule_id, "schedule_draining", {"reason": reason})
            cancelled = self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes SET state = 'cancelled'
                   WHERE schedule_id = %s AND state IN ('pending', 'ready')
                     AND node_id NOT LIKE 'cleanup:%%' RETURNING node_id""",
                (schedule_id,),
            ).fetchall()
            return tuple(str(item["node_id"]) for item in cancelled)

    @_serialized
    def reject_schedule(self, schedule_id: str, *, reason: str) -> None:
        """Terminalize active state whose persisted graph cannot be safely hydrated."""
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None:
                raise KeyError(f"unknown scheduler schedule: {schedule_id}")
            if schedule["status"] not in {"running", "paused", "cancelled"}:
                return
            attempts = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = 'cancelled', error = %s, completed_at = now()
                   WHERE schedule_id = %s AND state = 'running'
                   RETURNING id, generation""",
                (reason, schedule_id),
            ).fetchall()
            for attempt in attempts:
                self._release_message_claims(str(attempt["id"]), int(attempt["generation"]))
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes SET state = 'cancelled'
                   WHERE schedule_id = %s
                     AND state IN ('pending', 'ready', 'leased')""",
                (schedule_id,),
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET status = 'cancelled', updated_at = now() WHERE id = %s""",
                (schedule_id,),
            )
            self._append_event(schedule_id, "schedule_rejected", {"reason": reason})

    @_serialized
    def cancel_node(
        self,
        schedule_id: str,
        node_id: str,
        *,
        reason: str,
        checkpoint: dict[str, object],
        idempotency_key: str | None = None,
    ) -> bool:
        """Cancel one non-terminal node without closing its containing schedule."""
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            if idempotency_key is not None:
                existing = self.connection.execute(
                    """SELECT payload FROM merlin_storage.scheduler_events
                       WHERE schedule_id = %s AND type = 'cancelled'
                         AND payload->>'idempotency_key' = %s LIMIT 1""",
                    (schedule_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["payload"].get("node_id") != node_id
                        or existing["payload"].get("reason") != reason
                    ):
                        raise ValueError(
                            "cancellation identity conflicts with a different node or reason"
                        )
                    return True
            schedule = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None or schedule["status"] != "running":
                raise ValueError("schedule is unknown or terminal")
            node = self.connection.execute(
                """SELECT state FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = %s FOR UPDATE""",
                (schedule_id, node_id),
            ).fetchone()
            if node is None:
                raise KeyError(f"unknown scheduler node: {node_id}")
            if node["state"] in {"completed", "failed", "cancelled", "quarantined"}:
                return False
            attempts = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = 'cancelled', error = %s, completed_at = now()
                   WHERE schedule_id = %s AND node_id = %s AND state = 'running'
                   RETURNING id, generation""",
                (reason, schedule_id, node_id),
            ).fetchall()
            for attempt in attempts:
                self._release_message_claims(str(attempt["id"]), int(attempt["generation"]))
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = 'cancelled'
                   WHERE schedule_id = %s AND node_id = %s""",
                (schedule_id, node_id),
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                "cancelled",
                {
                    "node_id": node_id,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "attempt_ids": [str(attempt["id"]) for attempt in attempts],
                    "checkpoint_digest": checkpoint_digest,
                },
            )
            return True

    @_serialized
    def complete_schedule(self, schedule_id: str) -> None:
        """Persist natural terminal convergence after reduction and cleanup."""
        with self.connection.transaction():
            schedule = self.connection.execute(
                """SELECT status FROM merlin_storage.scheduler_schedules
                   WHERE id = %s FOR UPDATE""",
                (schedule_id,),
            ).fetchone()
            if schedule is None:
                raise KeyError(f"unknown schedule: {schedule_id}")
            if schedule["status"] == "completed":
                return
            if schedule["status"] != "running":
                raise ValueError("only a running schedule can complete naturally")
            open_nodes = self.connection.execute(
                """SELECT count(*) AS count FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s
                     AND state NOT IN ('completed', 'failed', 'cancelled', 'quarantined')""",
                (schedule_id,),
            ).fetchone()
            reducer = self.connection.execute(
                """SELECT state FROM merlin_storage.scheduler_nodes
                   WHERE schedule_id = %s AND node_id = 'reducer'""",
                (schedule_id,),
            ).fetchone()
            if open_nodes is None or open_nodes["count"] or reducer is None:
                raise ValueError("schedule cannot complete while work remains")
            if reducer["state"] != "completed":
                raise ValueError("schedule cannot complete before reducer success")
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET status = 'completed', updated_at = now() WHERE id = %s""",
                (schedule_id,),
            )
            self._append_event(schedule_id, "schedule_completed", {"reason": "reduced"})

    @_serialized
    def commit_reducer(
        self,
        attempt_id: str,
        generation: int,
        *,
        schema_id: str,
        result: dict[str, object],
        checkpoint: dict[str, object],
        messages: Sequence[tuple[str, str, str, dict[str, object]]] = (),
        applied_message_ids: Sequence[str] = (),
        target_session_id: str | None = None,
    ) -> str:
        """Atomically commit result, completion, messages, node state, and checkpoint."""
        target_session_id = _target_session_id(target_session_id)
        result_digest = _digest(result)
        checkpoint_jsonb, checkpoint_digest = _bounded_schedule_jsonb(
            checkpoint, artifact="checkpoint"
        )
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation)
            schedule_id = str(attempt["schedule_id"])
            node_id = attempt["node_id"]
            self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_results
                       (attempt_id, schedule_id, node_id, generation, schema_id,
                        payload, payload_digest)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (attempt_id) DO UPDATE SET
                       schema_id = excluded.schema_id,
                       payload = excluded.payload,
                       payload_digest = excluded.payload_digest""",
                (
                    attempt_id,
                    schedule_id,
                    node_id,
                    generation,
                    schema_id,
                    Jsonb(result),
                    result_digest,
                ),
            )
            updated = self.connection.execute(
                """UPDATE merlin_storage.scheduler_attempts
                   SET state = 'completed', completed_at = now(),
                       target_session_id = coalesce(target_session_id, %s)
                   WHERE id = %s
                     AND (%s::text IS NULL OR target_session_id IS NULL
                          OR target_session_id = %s)""",
                (target_session_id, attempt_id, target_session_id, target_session_id),
            )
            if updated.rowcount != 1:
                raise StaleLeaseError("attempt target session identity changed")
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_nodes
                   SET state = 'completed', result_digest = %s
                   WHERE schedule_id = %s AND node_id = %s AND lease_generation = %s""",
                (result_digest, schedule_id, node_id, generation),
            )
            for sender, recipient, message_schema, payload in messages:
                self._insert_message(schedule_id, sender, recipient, message_schema, payload)
            if applied_message_ids:
                message_ids = list(dict.fromkeys(applied_message_ids))
                acknowledged = self.connection.execute(
                    """UPDATE merlin_storage.scheduler_mailbox
                       SET acknowledged_at = coalesce(acknowledged_at, now())
                       WHERE id = ANY(%s) AND claimed_by_attempt = %s
                         AND claimed_generation = %s""",
                    (message_ids, attempt_id, generation),
                )
                if acknowledged.rowcount != len(message_ids):
                    raise StaleLeaseError("reducer mailbox claim is stale or incomplete")
            self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_completions
                       (attempt_id, schedule_id, node_id, result_digest)
                   VALUES (%s, %s, %s, %s)""",
                (attempt_id, schedule_id, node_id, result_digest),
            )
            self._record_baseline_stage(
                schedule_id, node_id, attempt_id, result_digest, target_session_id
            )
            self.connection.execute(
                """UPDATE merlin_storage.scheduler_schedules
                   SET checkpoint = %s, checkpoint_digest = %s, updated_at = now()
                   WHERE id = %s""",
                (checkpoint_jsonb, checkpoint_digest, schedule_id),
            )
            self._append_event(
                schedule_id,
                "completed",
                {
                    "node_id": node_id,
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "result_digest": result_digest,
                    "checkpoint_digest": checkpoint_digest,
                },
            )
        return result_digest

    def _record_baseline_stage(
        self,
        schedule_id: str,
        node_id: str,
        attempt_id: str,
        result_digest: str,
        target_session_id: str | None,
    ) -> None:
        schedule = self.connection.execute(
            "SELECT graph FROM merlin_storage.scheduler_schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if schedule is None or not node_id.startswith("worker:"):
            return
        assignment_id = node_id.removeprefix("worker:")
        assignments = schedule["graph"].get("assignments", ())
        assignment = next(
            (
                value
                for value in assignments
                if isinstance(value, Mapping) and value.get("id") == assignment_id
            ),
            None,
        )
        if not isinstance(assignment, Mapping) or assignment.get("execution_stage") is None:
            return
        stage = str(assignment["execution_stage"])
        agenda_id = str(assignment["agenda_id"])
        predecessor_stage = {"control": None, "direct": "control", "replay": "direct"}[stage]
        predecessor = None
        predecessor_assignment = None
        if predecessor_stage is not None:
            predecessor = self.connection.execute(
                """SELECT baseline.*, attempt.node_id AS scheduler_node_id
                   FROM merlin_storage.baseline_technique_attempts AS baseline
                   JOIN merlin_storage.scheduler_attempts AS attempt
                     ON attempt.id = baseline.scheduler_attempt_id
                   WHERE baseline.schedule_id = %s
                     AND baseline.agenda_id = %s
                     AND baseline.stage = %s""",
                (schedule_id, agenda_id, predecessor_stage),
            ).fetchone()
            predecessor_assignment_id = assignment.get("predecessor_assignment_id")
            predecessor_assignment = next(
                (
                    value
                    for value in assignments
                    if isinstance(value, Mapping) and value.get("id") == predecessor_assignment_id
                ),
                None,
            )
        prior_stage_sessions = self.connection.execute(
            """SELECT target_session_id
               FROM merlin_storage.baseline_technique_attempts
               WHERE schedule_id = %s AND agenda_id = %s
                 AND target_session_id IS NOT NULL""",
            (schedule_id, agenda_id),
        ).fetchall()
        all_effects = self.connection.execute(
            """SELECT * FROM merlin_storage.tool_effects
               WHERE attempt_id = %s AND effect IN ('target_network', 'target_read')
               ORDER BY created_at, idempotency_key""",
            (attempt_id,),
        ).fetchall()
        campaign_owner = self._baseline_campaign_owner(schedule_id)
        effects = tuple(
            effect
            for effect in all_effects
            if effect["state"] in {"acknowledged", "reconciled"}
            and effect["result_is_error"] is False
            and effect["target_id"] == assignment.get("target_profile_id")
            and (effect["result_metadata"] or {}).get("target_session_id") == target_session_id
            and (campaign_owner is None or effect["campaign_id"] == campaign_owner)
        )
        network_effects = tuple(
            effect for effect in effects if effect["effect"] == "target_network"
        )
        unsafe_effect = any(
            effect["effect"] == "target_network" and effect not in effects for effect in all_effects
        )
        exchanges: list[tuple[Any, Any, tuple[str, ...]]] = []
        browser_exchanges: dict[tuple[str, str], tuple[Any, Any, tuple[str, ...]]] = {}
        for index, effect in enumerate(effects):
            metadata = effect["result_metadata"] or {}
            if metadata.get("target_session_id") != target_session_id:
                continue
            target_exchanges = metadata.get("target_exchanges")
            if (
                effect["tool_name"] in {"target_chat", "target_request"}
                and isinstance(target_exchanges, list)
                and any(
                    isinstance(item, Mapping)
                    and item.get("interaction") is True
                    and item.get("cleanup") is not True
                    for item in target_exchanges
                )
            ):
                exchanges.append((effect, effect, (effect["idempotency_key"],)))
                continue
            if effect["tool_name"] not in {
                "browser_wait_for_text",
                "browser_snapshot",
                "browser_network_events",
            }:
                continue
            preceding = effects[:index]
            fill = next(
                (
                    item
                    for item in reversed(preceding)
                    if item["tool_name"] == "browser_fill"
                    and isinstance((item["result_metadata"] or {}).get("browser_action"), Mapping)
                    and item["result_metadata"]["browser_action"].get("kind") == "fill"
                ),
                None,
            )
            submit = next(
                (
                    item
                    for item in reversed(preceding)
                    if item["tool_name"] in {"browser_press", "browser_click"}
                    and isinstance((item["result_metadata"] or {}).get("browser_action"), Mapping)
                    and (
                        item["result_metadata"]["browser_action"].get("kind") == "click"
                        or (
                            item["result_metadata"]["browser_action"].get("kind") == "press"
                            and str(
                                item["result_metadata"]["browser_action"].get("value", "")
                            ).casefold()
                            in {"enter", "return"}
                        )
                    )
                ),
                None,
            )
            if fill is None or submit is None or preceding.index(fill) >= preceding.index(submit):
                continue
            try:
                filled = json.loads(fill["result_content"] or "")
                submitted = json.loads(submit["result_content"] or "")
                observed = json.loads(effect["result_content"] or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if not all(isinstance(item, Mapping) for item in (filled, submitted, observed)):
                continue
            if observed.get("target_id") != assignment.get("target_profile_id"):
                continue
            identities = {
                (item.get("context_id"), item.get("tab_ref"))
                for item in (filled, submitted, observed)
            }
            steps = [item.get("step") for item in (filled, submitted, observed)]
            if len(identities) != 1 or not all(isinstance(value, int) for value in steps):
                continue
            if not (steps[0] < steps[1] < steps[2]):
                continue
            exchange_key = (fill["idempotency_key"], submit["idempotency_key"])
            browser_exchanges[exchange_key] = (
                fill,
                effect,
                (
                    fill["idempotency_key"],
                    submit["idempotency_key"],
                    effect["idempotency_key"],
                ),
            )
        exchanges.extend(browser_exchanges.values())
        prompt_effect, response_effect, witness_effect_ids = (
            exchanges[0] if len(exchanges) == 1 else (None, None, ())
        )

        def effect_sequence(effect_ids: Sequence[str]) -> tuple[tuple[str, str], ...]:
            if not effect_ids:
                return ()
            rows = self.connection.execute(
                """SELECT tool_name, arguments_digest, result_metadata
                   FROM merlin_storage.tool_effects WHERE idempotency_key = ANY(%s)
                   ORDER BY array_position(%s::text[], idempotency_key)""",
                (list(effect_ids), list(effect_ids)),
            ).fetchall()
            return tuple(
                (
                    str(item["tool_name"]),
                    str(
                        (item["result_metadata"] or {}).get("target_arguments_digest")
                        or item["arguments_digest"]
                    ),
                )
                for item in rows
            )

        current_sequence = effect_sequence(witness_effect_ids)
        predecessor_sequence = (
            () if predecessor is None else effect_sequence(predecessor["witness_effect_ids"])
        )
        witness_effects = tuple(
            effect for effect in effects if effect["idempotency_key"] in witness_effect_ids
        )
        requires_remote_session_identity = any(
            effect["tool_name"] == "target_request" for effect in witness_effects
        )
        remote_target_session_ids = {
            str(remote_id)
            for effect in witness_effects
            if isinstance(
                remote_id := (effect["result_metadata"] or {}).get("remote_target_session_id"),
                str,
            )
            and remote_id
        }
        receipt_target_session_id = (
            next(iter(remote_target_session_ids))
            if len(remote_target_session_ids) == 1
            else None
            if requires_remote_session_identity
            else target_session_id
        )
        ambiguous = self.connection.execute(
            """SELECT 1 FROM merlin_storage.tool_effects
               WHERE attempt_id = %s AND state IN ('intent', 'ambiguous')
                 AND effect = 'target_network' LIMIT 1""",
            (attempt_id,),
        ).fetchone()
        unaccounted_effects = tuple(
            effect["idempotency_key"]
            for effect in network_effects
            if effect["idempotency_key"] not in witness_effect_ids
        )
        outcome, reason = "completed", None
        if predecessor_stage is not None and (
            predecessor is None or predecessor["outcome"] != "completed"
        ):
            outcome, reason = "ambiguous", "required predecessor receipt is not completed"
        elif predecessor is not None and (
            not isinstance(predecessor_assignment, Mapping)
            or predecessor_assignment.get("id")
            != str(predecessor["scheduler_node_id"]).removeprefix("worker:")
            or predecessor_assignment.get("execution_stage") != predecessor_stage
            or any(
                predecessor_assignment.get(key) != assignment.get(key)
                for key in (
                    "agenda_id",
                    "target_profile_id",
                    "reviewed_adapter_digest",
                    "pair_id",
                    "comparison_policy",
                    "semantic_technique_id",
                )
            )
            or predecessor["pair_id"] != assignment.get("pair_id")
            or predecessor["technique_id"] != assignment.get("semantic_technique_id")
        ):
            outcome, reason = "ambiguous", "reviewed target or comparison identity drifted"
        elif receipt_target_session_id is not None and any(
            row["target_session_id"] == receipt_target_session_id for row in prior_stage_sessions
        ):
            outcome, reason = "ambiguous", "PAIR stages reused one physical target session"
        elif requires_remote_session_identity and len(remote_target_session_ids) != 1:
            outcome, reason = "ambiguous", "physical target session identity is unresolved"
        elif unsafe_effect or ambiguous is not None or len(exchanges) > 1 or unaccounted_effects:
            outcome, reason = "ambiguous", "target effect is unresolved or outside exact witness"
        elif prompt_effect is None or response_effect is None or receipt_target_session_id is None:
            outcome, reason = "ambiguous", "qualifying attributable target receipt is missing"
        elif stage == "replay" and (
            bool(set(predecessor["witness_effect_ids"]) & set(witness_effect_ids))
            or predecessor_sequence != current_sequence
            or predecessor["oracle_digest"] != assignment["oracle_digest"]
        ):
            outcome, reason = "ambiguous", "exact replay binding diverged"
        values = (
            schedule_id,
            agenda_id,
            str(assignment["semantic_technique_id"]),
            str(assignment["pair_id"]),
            stage,
            attempt_id,
            None if predecessor is None else predecessor["id"],
            (
                None
                if prompt_effect is None
                else str(
                    (prompt_effect["result_metadata"] or {}).get("target_arguments_digest")
                    or prompt_effect["arguments_digest"]
                )
            ),
            str(assignment["oracle_digest"]),
            receipt_target_session_id,
            str(assignment["target_session_policy"]),
            None if prompt_effect is None else prompt_effect["idempotency_key"],
            None if response_effect is None else response_effect["idempotency_key"],
            list(witness_effect_ids),
            outcome,
            reason,
            result_digest,
        )
        inserted = self.connection.execute(
            """INSERT INTO merlin_storage.baseline_technique_attempts
                   (schedule_id, agenda_id, technique_id, pair_id, stage,
                    scheduler_attempt_id, predecessor_id, prompt_digest, oracle_digest,
                    target_session_id, target_session_policy, prompt_effect_id, target_effect_id,
                    witness_effect_ids, outcome, reason, result_digest)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (scheduler_attempt_id) DO NOTHING RETURNING id""",
            values,
        ).fetchone()
        if inserted is None:
            existing = self.connection.execute(
                """SELECT schedule_id::text, agenda_id, technique_id, pair_id, stage,
                          scheduler_attempt_id::text, coalesce(predecessor_id::text, ''),
                          prompt_digest, oracle_digest, target_session_id,
                          target_session_policy, prompt_effect_id, target_effect_id,
                          witness_effect_ids,
                          outcome, reason, result_digest
                   FROM merlin_storage.baseline_technique_attempts
                   WHERE scheduler_attempt_id = %s""",
                (attempt_id,),
            ).fetchone()
            expected = tuple("" if value is None else str(value) for value in values)
            actual = tuple("" if value is None else str(value) for value in existing or ())
            if existing is None or actual != expected:
                raise ValueError("terminal baseline stage receipt is immutable")

    def commit_partial(
        self,
        attempt_id: str,
        generation: int,
        *,
        result: dict[str, object],
        applied_message_ids: Sequence[str] = (),
    ) -> str:
        """Persist recoverable partial provider state without completing the attempt."""
        result_digest = _digest(result)
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation)
            previous = self.connection.execute(
                """SELECT payload_digest FROM merlin_storage.scheduler_results
                   WHERE attempt_id = %s""",
                (attempt_id,),
            ).fetchone()
            self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_results
                       (attempt_id, schedule_id, node_id, generation, schema_id,
                        payload, payload_digest)
                   VALUES (%s, %s, %s, %s, 'schema/partial-result', %s, %s)
                   ON CONFLICT (attempt_id) DO UPDATE SET
                       schema_id = excluded.schema_id,
                       payload = excluded.payload,
                       payload_digest = excluded.payload_digest""",
                (
                    attempt_id,
                    str(attempt["schedule_id"]),
                    attempt["node_id"],
                    generation,
                    Jsonb(result),
                    result_digest,
                ),
            )
            if applied_message_ids:
                message_ids = list(dict.fromkeys(applied_message_ids))
                acknowledged = self.connection.execute(
                    """UPDATE merlin_storage.scheduler_mailbox
                       SET acknowledged_at = coalesce(acknowledged_at, now())
                       WHERE id = ANY(%s) AND claimed_by_attempt = %s
                         AND claimed_generation = %s""",
                    (message_ids, attempt_id, generation),
                )
                if acknowledged.rowcount != len(message_ids):
                    raise StaleLeaseError("mailbox claim does not match the attempt generation")
            checkpoint = result.get("worker_checkpoint")
            checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            strategy_state = checkpoint.get("strategy_state")
            if (
                isinstance(strategy_state, Mapping)
                and strategy_state.get("schema") == "merlin.hydra-select-state.v1"
                and isinstance(strategy_state.get("plan"), Mapping)
            ):
                node_id = str(attempt["node_id"])
                if not node_id.startswith("worker:"):
                    raise ValueError("Hydra-Select plan checkpoint lacks a worker node")
                self._persist_hydra_select_plan(
                    str(attempt["schedule_id"]),
                    node_id.removeprefix("worker:"),
                    strategy_state,
                )
            messages = checkpoint.get("messages", ())
            messages = messages if isinstance(messages, list) else []
            last_message = messages[-1] if messages and isinstance(messages[-1], dict) else {}
            role = str(last_message.get("role", "working"))
            phase = (
                "sending" if role == "tool" else "thinking" if role == "assistant" else "planning"
            )
            if previous is None or previous["payload_digest"] != result_digest:
                self._append_event(
                    str(attempt["schedule_id"]),
                    "checkpointed",
                    {
                        "node_id": attempt["node_id"],
                        "attempt_id": attempt_id,
                        "generation": generation,
                        "phase": phase,
                        "elapsed_seconds": checkpoint.get("elapsed_seconds"),
                        "usage": checkpoint.get("usage", {}),
                        "action_count": len(checkpoint.get("action_history", ())),
                        "result_digest": result_digest,
                    },
                )
        return result_digest

    @_serialized
    def send_message(
        self,
        schedule_id: str,
        sender: str,
        recipient: str,
        schema_id: str,
        payload: dict[str, object],
    ) -> MailboxMessage:
        with self.connection.transaction():
            row = self._insert_message(schedule_id, sender, recipient, schema_id, payload)
            self._append_event(schedule_id, "message_sent", {"message_id": str(row["id"])})
        return _message(row)

    @_serialized
    def send_message_once(
        self,
        message_id: str,
        schedule_id: str,
        sender: str,
        recipient: str,
        schema_id: str,
        payload: dict[str, object],
    ) -> tuple[MailboxMessage, bool]:
        """Persist one deterministic mailbox delivery and report whether it was newly queued."""
        identifier = UUID(message_id)
        payload_digest = _digest(payload)
        with self.connection.transaction():
            row = self.connection.execute(
                """INSERT INTO merlin_storage.scheduler_mailbox
                       (id, schedule_id, sender, recipient, schema_id, payload, payload_digest)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING
                   RETURNING id, schedule_id, sender, recipient, schema_id, payload,
                             payload_digest""",
                (
                    identifier,
                    schedule_id,
                    sender,
                    recipient,
                    schema_id,
                    Jsonb(payload),
                    payload_digest,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = self.connection.execute(
                    """SELECT id, schedule_id, sender, recipient, schema_id, payload,
                              payload_digest
                       FROM merlin_storage.scheduler_mailbox WHERE id = %s""",
                    (identifier,),
                ).fetchone()
            if row is None:
                raise RuntimeError("deterministic mailbox delivery disappeared")
            if (
                str(row["schedule_id"]) != schedule_id
                or row["sender"] != sender
                or row["recipient"] != recipient
                or row["schema_id"] != schema_id
                or row["payload_digest"] != payload_digest
            ):
                raise ValueError("mailbox delivery identity conflicts with different content")
            if created:
                self._append_event(schedule_id, "message_sent", {"message_id": str(row["id"])})
        return _message(row), created

    @_serialized
    def get_message(self, message_id: str) -> MailboxMessage | None:
        row = self.connection.execute(
            """SELECT id, schedule_id, sender, recipient, schema_id, payload, payload_digest
               FROM merlin_storage.scheduler_mailbox WHERE id = %s""",
            (UUID(message_id),),
        ).fetchone()
        return _message(row) if row is not None else None

    @_serialized
    def claim_messages(
        self, attempt_id: str, generation: int, recipient: str, *, limit: int = 50
    ) -> list[MailboxMessage]:
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation)
            rows = self.connection.execute(
                """WITH selected AS (
                       SELECT id FROM merlin_storage.scheduler_mailbox
                       WHERE schedule_id = %s AND recipient = %s
                         AND claimed_by_attempt IS NULL AND acknowledged_at IS NULL
                       ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE merlin_storage.scheduler_mailbox AS message
                   SET claimed_by_attempt = %s, claimed_generation = %s
                   FROM selected WHERE message.id = selected.id
                   RETURNING message.id, message.schedule_id, message.sender, message.recipient,
                             message.schema_id, message.payload, message.payload_digest""",
                (attempt["schedule_id"], recipient, limit, attempt_id, generation),
            ).fetchall()
        return [_message(row) for row in rows]

    @_serialized
    def claim_message(
        self, attempt_id: str, generation: int, recipient: str, message_id: str
    ) -> MailboxMessage:
        """Bind one newly delivered message to the currently fenced attempt."""
        with self.connection.transaction():
            attempt = self._lock_attempt(attempt_id, generation)
            row = self.connection.execute(
                """UPDATE merlin_storage.scheduler_mailbox
                   SET claimed_by_attempt = %s, claimed_generation = %s
                   WHERE id = %s AND schedule_id = %s AND recipient = %s
                     AND claimed_by_attempt IS NULL AND acknowledged_at IS NULL
                   RETURNING id, schedule_id, sender, recipient, schema_id,
                             payload, payload_digest""",
                (attempt_id, generation, message_id, attempt["schedule_id"], recipient),
            ).fetchone()
            if row is None:
                raise StaleLeaseError("mailbox message is unavailable for this attempt")
        return _message(row)

    @_serialized
    def acknowledge_messages(
        self, attempt_id: str, generation: int, message_ids: Sequence[str]
    ) -> int:
        if not message_ids:
            return 0
        unique_ids = list(dict.fromkeys(message_ids))
        with self.connection.transaction():
            self._lock_attempt(attempt_id, generation)
            cursor = self.connection.execute(
                """UPDATE merlin_storage.scheduler_mailbox
                   SET acknowledged_at = coalesce(acknowledged_at, now())
                   WHERE id = ANY(%s) AND claimed_by_attempt = %s
                     AND claimed_generation = %s""",
                (unique_ids, attempt_id, generation),
            )
            if cursor.rowcount != len(unique_ids):
                raise StaleLeaseError("mailbox claim does not match the attempt generation")
        return cursor.rowcount

    @_serialized
    def acknowledge_completion(self, attempt_id: str) -> None:
        cursor = self.connection.execute(
            """UPDATE merlin_storage.scheduler_completions SET acknowledged_at = now()
               WHERE attempt_id = %s AND acknowledged_at IS NULL""",
            (attempt_id,),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise KeyError(f"unknown or acknowledged completion: {attempt_id}")
        self.connection.commit()

    @_serialized
    @_transactional
    def bind_technique_attempt(
        self,
        schedule_id: str,
        task_id: str,
        scheduler_attempt_id: str,
        *,
        technique_id: str,
        strategy_id: str,
        oracle_digest: str,
        control_kind: str,
        language: str = "und",
        transform_id: str | None = None,
        target_id: str | None = None,
        mutation_id: str | None = None,
        hypothesis_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        strategy_state: Mapping[str, object] | None = None,
    ) -> str | None:
        """Bind a campaign experiment before its first possible target effect."""

        wave = self.connection.execute(
            """SELECT campaign_id, wave FROM merlin_storage.campaign_waves
               WHERE schedule_id = %s""",
            (schedule_id,),
        ).fetchone()
        if wave is None:
            return None
        scheduler_attempt = self.connection.execute(
            """SELECT schedule_id, node_id, state, lease_expires_at > now() AS lease_unexpired
               FROM merlin_storage.scheduler_attempts
               WHERE id = %s
               FOR UPDATE""",
            (scheduler_attempt_id,),
        ).fetchone()
        expected_node_id = f"worker:{task_id}"
        if (
            scheduler_attempt is None
            or str(scheduler_attempt["schedule_id"]) != schedule_id
            or scheduler_attempt["node_id"] != expected_node_id
            or scheduler_attempt["state"] != "running"
            or scheduler_attempt["lease_unexpired"] is not True
        ):
            raise ValueError("scheduler attempt does not own the expected campaign task lease")
        candidate_identity = (
            strategy_state.get("candidate_identity")
            if isinstance(strategy_state, Mapping)
            else None
        )
        if candidate_identity is not None:
            candidate_fields = {
                "campaign_id",
                "target_id",
                "target_generation",
                "scenario_fingerprint",
                "technique_id",
                "controller_id",
                "payload_family",
                "requested_language",
                "mutation_id",
            }
            if (
                not isinstance(candidate_identity, Mapping)
                or set(candidate_identity) != candidate_fields
                or any(
                    not isinstance(candidate_identity.get(field), str)
                    or not str(candidate_identity[field]).strip()
                    for field in candidate_fields
                )
                or candidate_identity.get("campaign_id") != str(wave["campaign_id"])
                or not target_id
                or candidate_identity.get("target_id") != target_id
                or candidate_identity.get("technique_id") != technique_id
                or candidate_identity.get("controller_id") != strategy_id
                or str(candidate_identity.get("requested_language")).casefold()
                != language.casefold()
                or candidate_identity.get("payload_family") != (transform_id or "baseline")
                or not mutation_id
                or candidate_identity.get("mutation_id") != mutation_id
            ):
                raise ValueError(
                    "sealed candidate identity conflicts with the executed assignment axes"
                )
        identity = "\0".join(
            (
                str(wave["campaign_id"]),
                str(wave["wave"]),
                task_id,
                technique_id,
                strategy_id,
                control_kind,
                language,
                transform_id or "",
                target_id or "",
                mutation_id or "",
                hypothesis_id or "",
                oracle_digest,
            )
        )
        attempt_id = str(uuid5(NAMESPACE_URL, f"merlin:technique-attempt:{identity}"))
        repository = TechniqueAttemptRepository(self.connection)
        stored = repository.begin(
            TechniqueAttempt(
                id=attempt_id,
                campaign_id=str(wave["campaign_id"]),
                wave=int(wave["wave"]),
                task_id=task_id,
                technique_id=technique_id,
                strategy_id=strategy_id,
                hypothesis_id=hypothesis_id,
                transform_id=transform_id,
                language=language,
                control_kind=TechniqueAttemptControlKind(control_kind),
                oracle_digest=oracle_digest,
                scheduler_attempt_id=scheduler_attempt_id,
                provider=provider,
                model=model,
                strategy_state=dict(strategy_state or {}),
            )
        )
        if stored.state == TechniqueAttemptState.PLANNED:
            stored = repository.transition(
                attempt_id,
                TechniqueAttemptState.PLANNED,
                TechniqueAttemptState.ACTIVE,
                scheduler_attempt_id=scheduler_attempt_id,
                provider=provider,
                model=model,
            )
        elif stored.scheduler_attempt_id != scheduler_attempt_id:
            stored = repository.rebind_scheduler(attempt_id, scheduler_attempt_id)
        return stored.id

    @_serialized
    def observe_technique_attempt(
        self,
        attempt_id: str,
        *,
        evidence_refs: Sequence[str] = (),
        usage: Mapping[str, object] | None = None,
    ) -> None:
        TechniqueAttemptRepository(self.connection).observe(
            attempt_id, evidence_refs=tuple(evidence_refs), usage=usage
        )

    @_serialized
    def terminalize_technique_attempt(
        self,
        attempt_id: str,
        state: str,
        *,
        reason: str,
        evidence_refs: Sequence[str] = (),
        usage: Mapping[str, object] | None = None,
        strategy_state: Mapping[str, object] | None = None,
    ) -> None:
        TechniqueAttemptRepository(self.connection).terminalize(
            attempt_id,
            TechniqueAttemptState(state),
            reason=reason,
            evidence_refs=tuple(evidence_refs),
            usage=usage,
            strategy_state=strategy_state,
        )

    def list_technique_attempts(self, schedule_id: str) -> tuple[TechniqueAttempt, ...]:
        return TechniqueAttemptRepository(self.connection).list_for_schedule(schedule_id)

    @_serialized
    def get_technique_attempt(self, attempt_id: str) -> TechniqueAttempt | None:
        return TechniqueAttemptRepository(self.connection).get(attempt_id)

    def _release_message_claims(self, attempt_id: str, generation: int) -> None:
        """Make unacknowledged deliveries claimable after an attempt terminates."""
        self.connection.execute(
            """UPDATE merlin_storage.scheduler_mailbox
               SET claimed_by_attempt = NULL, claimed_generation = NULL
               WHERE claimed_by_attempt = %s AND claimed_generation = %s
                 AND acknowledged_at IS NULL""",
            (attempt_id, generation),
        )

    @_serialized
    def events(self, schedule_id: str) -> list[dict[str, Any]]:
        return self.connection.execute(
            """SELECT sequence, type, payload, previous_digest, digest, created_at
               FROM merlin_storage.scheduler_events WHERE schedule_id = %s ORDER BY sequence""",
            (schedule_id,),
        ).fetchall()

    @_serialized
    def verify(self, schedule_id: str) -> None:
        """Fail closed if graph, checkpoint, or append-only event digests diverge."""
        schedule = self.connection.execute(
            """SELECT graph, graph_digest, checkpoint, checkpoint_digest,
                      event_count, last_event_digest
               FROM merlin_storage.scheduler_schedules WHERE id = %s""",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(f"unknown schedule: {schedule_id}")
        if _digest(schedule["graph"]) != schedule["graph_digest"]:
            raise RuntimeError("scheduler graph digest diverged")
        if _digest(schedule["checkpoint"]) != schedule["checkpoint_digest"]:
            raise RuntimeError("scheduler checkpoint digest diverged")
        previous = self._zero_digest
        events = self.events(schedule_id)
        for expected_sequence, event in enumerate(events, start=1):
            expected = _digest(
                {
                    "sequence": expected_sequence,
                    "type": event["type"],
                    "payload": event["payload"],
                    "previous": previous,
                }
            )
            if (
                event["sequence"] != expected_sequence
                or event["previous_digest"] != previous
                or event["digest"] != expected
            ):
                raise RuntimeError("scheduler event chain diverged")
            previous = expected
        if schedule["event_count"] != len(events) or schedule["last_event_digest"] != previous:
            raise RuntimeError("scheduler checkpoint event head diverged")

    def _lock_attempt(
        self, attempt_id: str, generation: int, *, require_unexpired: bool = True
    ) -> dict[str, Any]:
        row = self.connection.execute(
            """SELECT attempt.id, attempt.schedule_id, attempt.node_id, attempt.generation,
                      attempt.owner, attempt.lease_expires_at
               FROM merlin_storage.scheduler_attempts AS attempt
               JOIN merlin_storage.scheduler_nodes AS node
                 ON node.schedule_id = attempt.schedule_id AND node.node_id = attempt.node_id
               WHERE attempt.id = %s AND attempt.generation = %s
                 AND attempt.state = 'running' AND node.state = 'leased'
                 AND node.lease_generation = attempt.generation
                 AND (%s = false OR attempt.lease_expires_at > now())
               FOR UPDATE OF attempt, node""",
            (attempt_id, generation, require_unexpired),
        ).fetchone()
        if row is None:
            raise StaleLeaseError("attempt does not own the current lease generation")
        return row

    def _insert_message(
        self,
        schedule_id: str,
        sender: str,
        recipient: str,
        schema_id: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        return self.connection.execute(
            """INSERT INTO merlin_storage.scheduler_mailbox
                   (id, schedule_id, sender, recipient, schema_id, payload, payload_digest)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id, schedule_id, sender, recipient, schema_id, payload, payload_digest""",
            (uuid4(), schedule_id, sender, recipient, schema_id, Jsonb(payload), _digest(payload)),
        ).fetchone()

    def _append_event(self, schedule_id: str, event_type: str, payload: dict[str, object]) -> str:
        schedule = self.connection.execute(
            """SELECT event_count, last_event_digest
               FROM merlin_storage.scheduler_schedules WHERE id = %s FOR UPDATE""",
            (schedule_id,),
        ).fetchone()
        if schedule is None:
            raise KeyError(f"unknown schedule: {schedule_id}")
        sequence = schedule["event_count"] + 1
        previous = schedule["last_event_digest"]
        digest = _digest(
            {"sequence": sequence, "type": event_type, "payload": payload, "previous": previous}
        )
        self.connection.execute(
            """INSERT INTO merlin_storage.scheduler_events
                   (schedule_id, sequence, type, payload, previous_digest, digest)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (schedule_id, sequence, event_type, Jsonb(payload), previous, digest),
        )
        self.connection.execute(
            """UPDATE merlin_storage.scheduler_schedules
               SET event_count = %s, last_event_digest = %s, updated_at = now() WHERE id = %s""",
            (sequence, digest, schedule_id),
        )
        return digest


def _goat_contract_value(assignment: Mapping[str, object]) -> dict[str, object] | None:
    raw = assignment.get("strategy_contract")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    try:
        values = {str(key): value for key, value in raw}  # type: ignore[misc]
        toolbox = json.loads(str(values["toolbox_json"]))
        contract = {
            "version": str(values["version"]),
            "semantic_technique_id": str(values["semantic_technique_id"]),
            "goal_digest": str(values["goal_digest"]),
            "toolbox": tuple((str(item[0]), str(item[1]), str(item[2])) for item in toolbox),
            "toolbox_catalog_digest": str(values["toolbox_catalog_digest"]),
            "terminal_oracle_digest": str(values["terminal_oracle_digest"]),
            "reviewed_adapter_digest": str(values["reviewed_adapter_digest"]),
            "attacker_route_digest": str(values["attacker_route_digest"]),
            "attacker_tool_transport": str(values["attacker_tool_transport"]),
            "target_profile_id": str(values["target_profile_id"]),
            "session_policy": str(values["session_policy"]),
            "modality": str(values["modality"]),
            "language": str(values["language"]),
            "target_turns": int(values["target_turns"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    digests = (
        contract["goal_digest"],
        contract["terminal_oracle_digest"],
        contract["reviewed_adapter_digest"],
        contract["attacker_route_digest"],
        *(digest for _name, digest, _definition in contract["toolbox"]),  # type: ignore[union-attr]
    )
    if (
        contract["version"] != "merlin.goat.v1"
        or contract["toolbox"] != trusted_goat_toolbox()
        or contract["toolbox_catalog_digest"] != GOAT_TOOLBOX_CATALOG_DIGEST
        or contract["attacker_tool_transport"] != "buffered"
        or any(not isinstance(item, str) or not _is_qualified_digest(item) for item in digests)
        or any(
            not str(definition).strip() or digest != _qualified_digest(definition)
            for _name, digest, definition in contract["toolbox"]  # type: ignore[union-attr]
        )
        or contract["session_policy"] != "same-reviewed-session"
        or contract["modality"] != "text"
        or not str(contract["language"]).strip()
        or not str(contract["target_profile_id"]).strip()
        or not 2 <= int(contract["target_turns"]) <= 64
    ):
        return None
    return contract


def _hydra_select_contract_value(
    assignment: Mapping[str, object],
) -> dict[str, object] | None:
    raw = assignment.get("strategy_contract")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    try:
        values = {str(key): value for key, value in raw}  # type: ignore[misc]
        mechanisms = json.loads(str(values["mechanisms_json"]))
        contract = {
            "version": str(values["version"]),
            "semantic_technique_id": str(values["semantic_technique_id"]),
            "goal_digest": str(values["goal_digest"]),
            "objective_oracle_digest": str(values["objective_oracle_digest"]),
            "reviewed_adapter_digest": str(values["reviewed_adapter_digest"]),
            "attacker_route_digest": str(values["attacker_route_digest"]),
            "attacker_tool_transport": str(values["attacker_tool_transport"]),
            "target_profile_id": str(values["target_profile_id"]),
            "session_policy": str(values["session_policy"]),
            "modality": str(values["modality"]),
            "language": str(values["language"]),
            "mechanisms": tuple((str(item[0]), str(item[1]), str(item[2])) for item in mechanisms),
            "mechanism_catalog_digest": str(values["mechanism_catalog_digest"]),
            "proposed_count": int(values["proposed_count"]),
            "selected_count": int(values["selected_count"]),
            "max_candidate_chars": int(values["max_candidate_chars"]),
            "stop_on_positive": str(values["stop_on_positive"]) == "true",
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        contract["version"] != "merlin.hydra-select.v1"
        or contract["mechanisms"] != trusted_hydra_select_mechanisms()
        or contract["mechanism_catalog_digest"] != HYDRA_SELECT_CATALOG_DIGEST
        or contract["attacker_tool_transport"] != "buffered"
        or contract["session_policy"] != "same-reviewed-session"
        or contract["modality"] != "text"
        or int(contract["proposed_count"]) != len(contract["mechanisms"])
        or not 1 <= int(contract["selected_count"]) <= int(contract["proposed_count"])
        or any(
            not _is_qualified_digest(str(contract[name]))
            for name in (
                "goal_digest",
                "objective_oracle_digest",
                "reviewed_adapter_digest",
                "attacker_route_digest",
            )
        )
    ):
        return None
    return contract


def _valid_goat_plan(value: object, toolbox_ids: tuple[str, ...]) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "observation",
        "thought",
        "strategy",
        "response",
    }:
        return False
    strategies = value.get("strategy")
    return bool(
        isinstance(strategies, list)
        and strategies
        and len(set(strategies)) == len(strategies)
        and all(isinstance(item, str) and item in toolbox_ids for item in strategies)
        and all(
            isinstance(value.get(name), str)
            and bool(str(value[name]).strip())
            and len(str(value[name])) <= 16_384
            for name in ("observation", "thought", "response")
        )
    )


def _campaign_id_from_assignment(assignment_id: str) -> str:
    parts = assignment_id.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 and parts[0] == "campaign" else parts[0]


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _target_session_id(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("target session id must be non-blank when observed")
    return value


def _schedule_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "parent_schedule_id": str(row["parent_schedule_id"])
        if row.get("parent_schedule_id") is not None
        else None,
        "parent_attempt_id": str(row["parent_attempt_id"])
        if row.get("parent_attempt_id") is not None
        else None,
    }


def _message(row: dict[str, Any]) -> MailboxMessage:
    return MailboxMessage(
        id=str(row["id"]),
        schedule_id=str(row["schedule_id"]),
        sender=row["sender"],
        recipient=row["recipient"],
        schema_id=row["schema_id"],
        payload=row["payload"],
        payload_digest=row["payload_digest"],
    )
