from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import tempfile
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from merlin_agents.target_reobservation import target_reobservation_schedule_ids

SCHEMA_VERSION = "merlin.campaign-diagnostics.v1"
EVENT_SCHEMA_VERSION = "merlin.campaign-diagnostic-event.v1"
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_STRING_CHARS = 512

_MEMBER_CAPS = {
    "manifest.json": 128 * 1024,
    "campaign.json": 128 * 1024,
    "timeline.jsonl": 8 * 1024 * 1024,
    "workers.jsonl": 3 * 1024 * 1024,
    "effects.jsonl": 2 * 1024 * 1024,
    "usage.json": 1024 * 1024,
}
_ROW_CAPS = {
    "waves": 2_048,
    "timeline.jsonl": 20_000,
    "workers.jsonl": 10_000,
    "effects.jsonl": 2_000,
    "usage_schedules": 2_048,
}

_EFFECT_RECEIPT_FIELDS = (
    "target_effect_dispatch_status",
    "effect_applied",
    "target_effect_attempted",
    "interaction_count",
    "limit_interaction_count",
    "target_call_budget_charge",
    "target_session_id",
)
_EFFECT_BOOLEAN_FIELDS = (
    "effect_applied",
    "target_effect_attempted",
    "effect_ambiguous",
    "replacement_blocked",
    "target_session_identity_mismatch",
    "target_session_identity_unavailable",
)
_EFFECT_INTEGER_FIELDS = (
    "interaction_count",
    "limit_interaction_count",
    "target_call_budget_charge",
)
_EFFECT_CODE_FIELDS = (
    "target_effect_dispatch_status",
    "failure_code",
    "infrastructure_failure_category",
)

_CAMPAIGN_STRING_FIELDS = (
    "status",
    "manifest_digest",
    "schedule_id",
    "stop_reason",
    "pause_cause",
    "lifecycle_pause_reason",
    "lifecycle_resume_status",
)
_CAMPAIGN_INTEGER_FIELDS = ("event_count",)
_WAVE_STRING_FIELDS = ("kind", "schedule_id", "status")
_WAVE_INTEGER_FIELDS = ("wave",)
_LEAD_STRING_FIELDS = (
    "kind",
    "schema",
    "state",
    "status",
    "component",
    "phase",
    "wave_kind",
    "failure_kind",
    "error_code",
    "error_type",
    "infrastructure_failure_category",
)
_LEAD_INTEGER_FIELDS = (
    "tasks",
    "terminal_tasks",
    "target_calls",
    "model_steps",
    "completed_chunks",
    "output_characters",
    "consecutive_failures",
    "attempt",
    "generation",
    "count",
)
_LEAD_NUMBER_FIELDS = ("elapsed_seconds", "elapsed_ms", "latency_ms")
_LEAD_BOOLEAN_FIELDS = ("retryable", "blocking", "advisory", "permanent")
_SCHEDULER_STRING_FIELDS = (
    "node_id",
    "attempt_id",
    "replaced_attempt_id",
    "replacement_attempt_id",
    "status",
    "state",
    "failure_kind",
    "error_code",
    "infrastructure_failure_category",
)
_SCHEDULER_INTEGER_FIELDS = (
    "generation",
    "attempts_started",
    "failed_attempts",
    "completed",
    "failed",
    "cancelled",
    "count",
)
_SCHEDULER_BOOLEAN_FIELDS = ("retryable",)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "estimated_input_tokens",
    "estimated_output_tokens",
    "estimated_total_tokens",
    "reasoning_output_tokens",
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "inference_count",
    "incomplete_inference_count",
    "turns",
    "model_steps",
    "target_calls",
    "estimated_cost_usd",
    "elapsed_ms",
)

_OMITTED_CONTENT = (
    "objective",
    "URLs",
    "local paths",
    "hosts",
    "prompts",
    "reasoning",
    "model output",
    "tool arguments and results",
    "target evidence and screenshots",
    "mailbox data",
    "raw checkpoints",
    "reports",
    "credentials and secrets",
)


class CampaignDiagnosticsExporter:
    """Export a bounded structural projection of one durable campaign."""

    def __init__(
        self,
        campaign_store: Any,
        scheduler_store: Any,
        usage_loader: Callable[[str], Mapping[str, object] | Awaitable[Mapping[str, object]]],
        effect_loader: Callable[..., object] | None = None,
    ) -> None:
        self._campaign_store = campaign_store
        self._scheduler_store = scheduler_store
        self._usage_loader = usage_loader
        self._effect_loader = effect_loader

    async def export(self, campaign_id: str, output: str) -> Mapping[str, object]:
        campaign = await asyncio.to_thread(self._campaign_store.get, campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {campaign_id!r}")

        waves = list(await asyncio.to_thread(self._campaign_store.list_waves, campaign_id))
        lead = list(await asyncio.to_thread(self._campaign_store.list_lead, campaign_id))
        schedule_ids, schedule_discovery_errors = _schedule_ids(campaign, waves, lead)
        schedules: list[Mapping[str, object]] = []
        events_by_schedule: dict[str, Sequence[Mapping[str, object]]] = {}
        nodes_by_schedule: dict[str, Sequence[Mapping[str, object]]] = {}
        attempts_by_schedule: dict[str, Sequence[Mapping[str, object]]] = {}
        usage_by_schedule: dict[str, Mapping[str, object]] = {}
        source_errors = list(schedule_discovery_errors)
        for schedule_id in schedule_ids:
            try:
                schedule = await asyncio.to_thread(self._scheduler_store.load_schedule, schedule_id)
            except Exception as error:
                source_errors.append(_source_error("schedule", "load", error, schedule_id))
                continue
            if schedule is None:
                source_errors.append(
                    {
                        "source": "schedule",
                        "stage": "load",
                        "error_type": "NotFound",
                        "schedule_id": schedule_id,
                    }
                )
                continue
            schedules.append(schedule)
            for source, method, destination in (
                ("scheduler_events", self._scheduler_store.events, events_by_schedule),
                ("scheduler_nodes", self._scheduler_store.list_nodes, nodes_by_schedule),
                (
                    "scheduler_attempts",
                    self._scheduler_store.list_attempts,
                    attempts_by_schedule,
                ),
            ):
                try:
                    destination[schedule_id] = await asyncio.to_thread(method, schedule_id)
                except Exception as error:
                    source_errors.append(_source_error(source, "load", error, schedule_id))
            try:
                loaded_usage = self._usage_loader(schedule_id)
                if inspect.isawaitable(loaded_usage):
                    loaded_usage = await loaded_usage
                if not isinstance(loaded_usage, Mapping):
                    raise TypeError("schedule usage is not a mapping")
                usage_by_schedule[schedule_id] = loaded_usage
            except Exception as error:
                source_errors.append(_source_error("schedule_usage", "load", error, schedule_id))

        effects: Sequence[Mapping[str, object]] = ()
        effects_source_truncated = False
        if self._effect_loader is not None:
            try:
                loaded_effects = self._effect_loader(
                    campaign_id,
                    limit=_ROW_CAPS["effects.jsonl"] + 1,
                )
                if inspect.isawaitable(loaded_effects):
                    loaded_effects = await loaded_effects
                normalized_effects = tuple(
                    _record_mapping(effect)
                    for effect in loaded_effects  # type: ignore[union-attr]
                )
                effects_source_truncated = len(normalized_effects) > _ROW_CAPS["effects.jsonl"]
                effects = normalized_effects[-_ROW_CAPS["effects.jsonl"] :]
            except Exception as error:
                source_errors.append(_source_error("tool_effects", "load", error))

        tracker: dict[str, int] = {"strings": 0, "heartbeats_aggregated": 0}
        wave_by_schedule = {
            str(row.get("schedule_id")): int(row.get("wave", 0))
            for row in waves
            if row.get("schedule_id") is not None
        }
        campaign_document, wave_stats = _campaign_document(campaign, waves, tracker)
        timeline = _timeline_rows(
            campaign,
            waves,
            lead,
            schedules,
            events_by_schedule,
            wave_by_schedule,
            tracker,
        )
        workers = _worker_rows(
            campaign_id,
            nodes_by_schedule,
            attempts_by_schedule,
            wave_by_schedule,
            tracker,
        )
        effect_rows = _effect_rows(campaign_id, effects, tracker)
        usage_document, usage_stats = _usage_document(
            campaign_id,
            lead,
            usage_by_schedule,
            wave_by_schedule,
            tracker,
        )

        campaign_bytes, retained_waves = _bounded_document_rows(
            campaign_document, "waves", _MEMBER_CAPS["campaign.json"]
        )
        wave_stats = {
            **wave_stats,
            "retained": retained_waves,
            "dropped": int(wave_stats["input"]) - retained_waves,
        }
        timeline_bytes, timeline_stats = _bounded_jsonl(
            timeline,
            max_rows=_ROW_CAPS["timeline.jsonl"],
            max_bytes=_MEMBER_CAPS["timeline.jsonl"],
        )
        worker_bytes, worker_stats = _bounded_jsonl(
            workers,
            max_rows=_ROW_CAPS["workers.jsonl"],
            max_bytes=_MEMBER_CAPS["workers.jsonl"],
        )
        effect_bytes, effect_stats = _bounded_jsonl_recent(
            effect_rows,
            max_rows=_ROW_CAPS["effects.jsonl"],
            max_bytes=_MEMBER_CAPS["effects.jsonl"],
        )
        effect_stats = {
            **effect_stats,
            "source_truncated": effects_source_truncated,
        }
        usage_bytes, retained_usage_schedules = _bounded_document_rows(
            usage_document, "schedules", _MEMBER_CAPS["usage.json"]
        )
        usage_stats = {
            **usage_stats,
            "retained": retained_usage_schedules,
            "dropped": int(usage_stats["input"]) - retained_usage_schedules,
        }

        truncation = {
            "strings": tracker["strings"],
            "waves": wave_stats,
            "timeline": timeline_stats,
            "workers": worker_stats,
            "effects": effect_stats,
            "usage_schedules": usage_stats,
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": _bounded_string(campaign_id, tracker),
            "privacy": {
                "classification": "local-only",
                "shareable": False,
                "reason": (
                    "The bundle contains durable internal identifiers and failure metadata; "
                    "no field-level shareability review is asserted."
                ),
            },
            "ordering": {
                "total_causal_order": False,
                "rule": "Preserve sequence within each durable source only.",
            },
            "pre_prepare_correlation": {
                "available": False,
                "limitation": (
                    "Failures before a campaign is durably prepared have no campaign_id and "
                    "cannot be included in a campaign-scoped export."
                ),
            },
            "schedule_discovery": {
                "wave_schedules": True,
                "authenticated_target_reobservation_schedules": True,
                "other_auxiliary_schedules": False,
            },
            "source_errors": source_errors,
            "included_members": tuple(_MEMBER_CAPS),
            "omitted_content": _OMITTED_CONTENT,
            "caps": {
                "max_string_chars": MAX_STRING_CHARS,
                "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
                "member_bytes": dict(_MEMBER_CAPS),
                "rows": dict(_ROW_CAPS),
            },
            "aggregation": {
                "heartbeat_source_rows": tracker["heartbeats_aggregated"],
                "heartbeat_summary_rows": sum(
                    1 for row in timeline if row.get("type") == "scheduler.heartbeat_summary"
                ),
            },
            "truncation": truncation,
            "uncompressed_bytes": 0,
        }
        member_payloads = {
            "campaign.json": campaign_bytes,
            "timeline.jsonl": timeline_bytes,
            "workers.jsonl": worker_bytes,
            "effects.jsonl": effect_bytes,
            "usage.json": usage_bytes,
        }
        manifest_bytes = _manifest_bytes(manifest, member_payloads)
        member_payloads = {"manifest.json": manifest_bytes, **member_payloads}
        uncompressed_bytes = sum(len(value) for value in member_payloads.values())
        if uncompressed_bytes > MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("campaign diagnostics exceeded the 16 MiB uncompressed cap")

        path = Path(output).expanduser().resolve()  # noqa: ASYNC240
        if path.suffix.casefold() != ".zip":
            path = path.with_suffix(".zip")
        if path.exists():
            raise FileExistsError(f"campaign diagnostics already exists: {path}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for name, payload in member_payloads.items():
                    bundle.writestr(name, payload)
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"campaign diagnostics already exists: {path}") from None
        finally:
            os.unlink(temporary)
        return {
            "campaign_id": campaign_id,
            "output": str(path),
            "schema_version": SCHEMA_VERSION,
            "uncompressed_bytes": uncompressed_bytes,
            "member_count": len(member_payloads),
            "truncation": truncation,
        }


def _schedule_ids(
    campaign: Mapping[str, object],
    waves: Sequence[Mapping[str, object]],
    lead: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, ...], tuple[Mapping[str, str], ...]]:
    values = {
        str(value)
        for value in (
            campaign.get("schedule_id"),
            *(wave.get("schedule_id") for wave in waves),
        )
        if value is not None and str(value)
    }
    errors: list[Mapping[str, str]] = []
    envelope = campaign.get("manifest")
    reviewed = envelope.get("manifest", envelope) if isinstance(envelope, Mapping) else None
    target = reviewed.get("target") if isinstance(reviewed, Mapping) else None
    target_id = target.get("id") if isinstance(target, Mapping) else None
    session_id = campaign.get("session_id")
    campaign_id = campaign.get("id")
    if all(isinstance(value, str) and value for value in (campaign_id, session_id, target_id)):
        try:
            values.update(
                target_reobservation_schedule_ids(
                    lead,
                    campaign_id=campaign_id,
                    session_id=session_id,
                    target_id=target_id,
                )
            )
        except Exception as error:
            errors.append(
                _source_error(
                    "target_reobservation_journal",
                    "schedule_discovery",
                    error,
                )
            )
    return tuple(sorted(values)), tuple(errors)


def _campaign_document(
    campaign: Mapping[str, object],
    waves: Sequence[Mapping[str, object]],
    tracker: dict[str, int],
) -> tuple[dict[str, object], Mapping[str, int]]:
    safe_waves = []
    for row in sorted(waves, key=lambda item: int(item.get("wave", 0))):
        safe = {
            "wave": _integer(row.get("wave")),
            "created_at": _timestamp(row.get("created_at"), tracker),
            "updated_at": _timestamp(row.get("updated_at"), tracker),
        }
        _copy_fields(safe, row, _WAVE_STRING_FIELDS, "string", tracker)
        _copy_fields(safe, row, _WAVE_INTEGER_FIELDS, "integer", tracker)
        safe_waves.append(_without_none(safe))
    retained_waves = safe_waves[: _ROW_CAPS["waves"]]
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": _bounded_string(campaign.get("id"), tracker),
        "session_id": _bounded_string(campaign.get("session_id"), tracker),
        "created_at": _timestamp(campaign.get("created_at"), tracker),
        "updated_at": _timestamp(campaign.get("updated_at"), tracker),
        "waves": retained_waves,
    }
    _copy_fields(document, campaign, _CAMPAIGN_STRING_FIELDS, "string", tracker)
    _copy_fields(document, campaign, _CAMPAIGN_INTEGER_FIELDS, "integer", tracker)
    return _without_none(document), {
        "input": len(safe_waves),
        "retained": len(retained_waves),
        "dropped": len(safe_waves) - len(retained_waves),
    }


def _timeline_rows(
    campaign: Mapping[str, object],
    waves: Sequence[Mapping[str, object]],
    lead: Sequence[Mapping[str, object]],
    schedules: Sequence[Mapping[str, object]],
    events_by_schedule: Mapping[str, Sequence[Mapping[str, object]]],
    wave_by_schedule: Mapping[str, int],
    tracker: dict[str, int],
) -> list[dict[str, object]]:
    campaign_id = _bounded_string(campaign.get("id"), tracker)
    rows: list[dict[str, object]] = [
        _without_none(
            {
                "schema_version": EVENT_SCHEMA_VERSION,
                "source": "campaign",
                "source_id": campaign_id,
                "source_sequence": 1,
                "occurred_at": _timestamp(campaign.get("created_at"), tracker),
                "category": "prepare",
                "type": "campaign.prepared",
                "campaign_id": campaign_id,
                "data": _without_none(
                    {
                        "status": _bounded_string(campaign.get("status"), tracker),
                        "manifest_digest": _bounded_string(
                            campaign.get("manifest_digest"), tracker
                        ),
                    }
                ),
            }
        )
    ]
    for wave in sorted(waves, key=lambda item: int(item.get("wave", 0))):
        data: dict[str, object] = {}
        _copy_fields(data, wave, _WAVE_STRING_FIELDS, "string", tracker)
        rows.append(
            _without_none(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "wave",
                    "source_id": campaign_id,
                    "source_sequence": _integer(wave.get("wave")),
                    "occurred_at": _timestamp(wave.get("created_at"), tracker),
                    "category": "wave",
                    "type": "wave.recorded",
                    "campaign_id": campaign_id,
                    "wave": _integer(wave.get("wave")),
                    "schedule_id": _bounded_string(wave.get("schedule_id"), tracker),
                    "data": data,
                }
            )
        )
    for record in sorted(lead, key=lambda item: int(item.get("sequence", 0))):
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        kind = _bounded_string(payload.get("kind") or "lead.event", tracker)
        data: dict[str, object] = {}
        _copy_fields(data, payload, _LEAD_STRING_FIELDS, "string", tracker)
        _copy_fields(data, payload, _LEAD_INTEGER_FIELDS, "integer", tracker)
        _copy_fields(data, payload, _LEAD_NUMBER_FIELDS, "number", tracker)
        _copy_fields(data, payload, _LEAD_BOOLEAN_FIELDS, "boolean", tracker)
        rows.append(
            _without_none(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "lead_journal",
                    "source_id": campaign_id,
                    "source_sequence": _integer(record.get("sequence")),
                    "occurred_at": _timestamp(record.get("created_at"), tracker),
                    "category": _lead_category(str(kind)),
                    "type": f"lead.{kind}",
                    "campaign_id": campaign_id,
                    "wave": _integer(record.get("wave")),
                    "data": data,
                }
            )
        )
    for schedule in sorted(schedules, key=lambda item: str(item.get("id", ""))):
        schedule_id = _bounded_string(schedule.get("id"), tracker)
        rows.append(
            _without_none(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "schedule",
                    "source_id": schedule_id,
                    "source_sequence": 0,
                    "occurred_at": _timestamp(schedule.get("created_at"), tracker),
                    "category": "scheduler",
                    "type": "scheduler.snapshot",
                    "campaign_id": campaign_id,
                    "wave": wave_by_schedule.get(str(schedule_id)),
                    "schedule_id": schedule_id,
                    "data": _without_none(
                        {
                            "status": _bounded_string(schedule.get("status"), tracker),
                            "event_count": _integer(schedule.get("event_count")),
                        }
                    ),
                }
            )
        )
        schedule_events = events_by_schedule.get(str(schedule_id), ())
        rows.extend(
            _scheduler_timeline(
                campaign_id,
                str(schedule_id),
                wave_by_schedule.get(str(schedule_id)),
                schedule_events,
                tracker,
            )
        )
    return rows


def _scheduler_timeline(
    campaign_id: str,
    schedule_id: str,
    wave: int | None,
    events: Sequence[Mapping[str, object]],
    tracker: dict[str, int],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    heartbeat_groups: dict[tuple[str, int], dict[str, object]] = {}
    for event in sorted(events, key=lambda item: int(item.get("sequence", 0))):
        event_type = _bounded_string(event.get("type"), tracker)
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        sequence = _integer(event.get("sequence")) or 0
        if event_type == "heartbeat":
            attempt_id = _bounded_string(payload.get("attempt_id"), tracker)
            generation = _integer(payload.get("generation")) or 0
            key = (str(attempt_id or "unknown"), generation)
            current = heartbeat_groups.get(key)
            if current is None:
                heartbeat_groups[key] = {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "scheduler_event",
                    "source_id": schedule_id,
                    "source_sequence_start": sequence,
                    "source_sequence_end": sequence,
                    "occurred_at_first": _timestamp(event.get("created_at"), tracker),
                    "occurred_at_last": _timestamp(event.get("created_at"), tracker),
                    "category": "scheduler",
                    "type": "scheduler.heartbeat_summary",
                    "campaign_id": campaign_id,
                    "wave": wave,
                    "schedule_id": schedule_id,
                    "data": {
                        "attempt_id": attempt_id,
                        "generation": generation,
                        "count": 1,
                    },
                }
            else:
                current["source_sequence_end"] = sequence
                current["occurred_at_last"] = _timestamp(event.get("created_at"), tracker)
                data = current["data"]
                assert isinstance(data, dict)
                data["count"] = int(data["count"]) + 1
            tracker["heartbeats_aggregated"] += 1
            continue
        data: dict[str, object] = {}
        _copy_fields(data, payload, _SCHEDULER_STRING_FIELDS, "string", tracker)
        _copy_fields(data, payload, _SCHEDULER_INTEGER_FIELDS, "integer", tracker)
        _copy_fields(data, payload, _SCHEDULER_BOOLEAN_FIELDS, "boolean", tracker)
        rows.append(
            _without_none(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "source": "scheduler_event",
                    "source_id": schedule_id,
                    "source_sequence": sequence,
                    "occurred_at": _timestamp(event.get("created_at"), tracker),
                    "category": _scheduler_category(str(event_type)),
                    "type": f"scheduler.{event_type}",
                    "campaign_id": campaign_id,
                    "wave": wave,
                    "schedule_id": schedule_id,
                    "data": data,
                }
            )
        )
    rows.extend(heartbeat_groups.values())
    return sorted(
        rows,
        key=lambda row: int(row.get("source_sequence", row.get("source_sequence_start", 0))),
    )


def _worker_rows(
    campaign_id: str,
    nodes_by_schedule: Mapping[str, Sequence[Mapping[str, object]]],
    attempts_by_schedule: Mapping[str, Sequence[Mapping[str, object]]],
    wave_by_schedule: Mapping[str, int],
    tracker: dict[str, int],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for schedule_id in sorted(nodes_by_schedule):
        for node in sorted(
            nodes_by_schedule[schedule_id], key=lambda item: str(item.get("node_id", ""))
        ):
            row: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "node",
                "campaign_id": campaign_id,
                "wave": wave_by_schedule.get(schedule_id),
                "schedule_id": _bounded_string(schedule_id, tracker),
            }
            _copy_fields(row, node, ("node_id", "state"), "string", tracker)
            _copy_fields(
                row,
                node,
                ("lease_generation", "attempts_started", "failed_attempts"),
                "integer",
                tracker,
            )
            rows.append(_without_none(row))
        for attempt in sorted(
            attempts_by_schedule.get(schedule_id, ()),
            key=lambda item: (str(item.get("started_at", "")), str(item.get("id", ""))),
        ):
            row = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "attempt",
                "campaign_id": campaign_id,
                "wave": wave_by_schedule.get(schedule_id),
                "schedule_id": _bounded_string(schedule_id, tracker),
                "attempt_id": _bounded_string(attempt.get("id"), tracker),
                "node_id": _bounded_string(attempt.get("node_id"), tracker),
                "generation": _integer(attempt.get("generation")),
                "state": _bounded_string(attempt.get("state"), tracker),
                "failure_kind": _bounded_string(attempt.get("failure_kind"), tracker),
                "replaced_attempt_id": _bounded_string(attempt.get("replaced_attempt_id"), tracker),
                "started_at": _timestamp(attempt.get("started_at"), tracker),
                "heartbeat_at": _timestamp(attempt.get("heartbeat_at"), tracker),
                "completed_at": _timestamp(attempt.get("completed_at"), tracker),
            }
            rows.append(_without_none(row))
    return rows


def _effect_rows(
    campaign_id: str,
    effects: Sequence[Mapping[str, object]],
    tracker: dict[str, int],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for sequence, effect in enumerate(effects, start=1):
        metadata = effect.get("result_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        present = [field for field in _EFFECT_RECEIPT_FIELDS if field in metadata]
        receipt: dict[str, object] = {
            "fields_present": present,
            "fields_missing": [field for field in _EFFECT_RECEIPT_FIELDS if field not in metadata],
        }
        _copy_fields(receipt, metadata, _EFFECT_BOOLEAN_FIELDS, "boolean", tracker)
        _copy_fields(receipt, metadata, _EFFECT_INTEGER_FIELDS, "integer", tracker)
        for field in _EFFECT_CODE_FIELDS:
            if (value := _safe_code(metadata.get(field))) is not None:
                receipt[field] = value
        target_session_id = metadata.get("target_session_id")
        if isinstance(target_session_id, str) and target_session_id:
            receipt["target_session_id"] = _bounded_string(target_session_id, tracker)
        row = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "tool_effect",
            "campaign_id": campaign_id,
            "source_sequence": sequence,
            "idempotency_key": _bounded_string(effect.get("idempotency_key"), tracker),
            "call_id": _bounded_string(effect.get("call_id"), tracker),
            "agent_id": _bounded_string(effect.get("agent_id"), tracker),
            "attempt_id": _bounded_string(effect.get("attempt_id"), tracker),
            "tool_name": _safe_code(effect.get("tool_name")),
            "effect": _safe_code(effect.get("effect")),
            "arguments_digest": _bounded_string(effect.get("arguments_digest"), tracker),
            "state": _safe_code(effect.get("state")),
            "result_is_error": (
                effect.get("result_is_error")
                if isinstance(effect.get("result_is_error"), bool)
                else None
            ),
            "arguments_payload_present": effect.get("arguments_payload") is not None,
            "result_content_present": effect.get("result_content") is not None,
            "reconciliation_present": effect.get("reconciliation") is not None,
            "created_at": _timestamp(effect.get("created_at"), tracker),
            "updated_at": _timestamp(effect.get("updated_at"), tracker),
            "receipt": receipt,
        }
        rows.append(_without_none(row))
    return rows


def _usage_document(
    campaign_id: str,
    lead: Sequence[Mapping[str, object]],
    usage_by_schedule: Mapping[str, Mapping[str, object]],
    wave_by_schedule: Mapping[str, int],
    tracker: dict[str, int],
) -> tuple[dict[str, object], Mapping[str, int]]:
    lead_total: dict[str, int | float] = {}
    lead_records = 0
    for record in lead:
        payload = record.get("payload")
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        safe = _usage_values(usage)
        if safe:
            lead_records += 1
            _add_usage(lead_total, safe)
    schedule_rows = []
    campaign_total: dict[str, int | float] = dict(lead_total)
    for schedule_id in sorted(usage_by_schedule):
        usage = _usage_values(usage_by_schedule[schedule_id])
        _add_usage(campaign_total, usage)
        schedule_rows.append(
            {
                "schedule_id": _bounded_string(schedule_id, tracker),
                "wave": wave_by_schedule.get(schedule_id),
                "usage": usage,
            }
        )
    retained = schedule_rows[: _ROW_CAPS["usage_schedules"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "lead": {"records": lead_records, "usage": lead_total},
        "schedules": retained,
        "campaign_total": campaign_total,
    }, {
        "input": len(schedule_rows),
        "retained": len(retained),
        "dropped": len(schedule_rows) - len(retained),
    }


def _usage_values(value: Mapping[str, object]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key in _USAGE_FIELDS:
        item = value.get(key)
        safe = _number(item)
        if safe is not None:
            result[key] = safe
    return result


def _add_usage(total: dict[str, int | float], usage: Mapping[str, int | float]) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


def _copy_fields(
    target: dict[str, object],
    source: Mapping[str, object],
    fields: Sequence[str],
    kind: str,
    tracker: dict[str, int],
) -> None:
    for key in fields:
        value = source.get(key)
        if kind == "string":
            safe = _bounded_string(value, tracker)
        elif kind == "integer":
            safe = _integer(value)
        elif kind == "number":
            safe = _number(value)
        elif kind == "boolean":
            safe = value if isinstance(value, bool) else None
        else:
            raise AssertionError(kind)
        if safe is not None:
            target[key] = safe


def _bounded_string(value: object, tracker: dict[str, int]) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= MAX_STRING_CHARS:
        return text
    tracker["strings"] += 1
    return text[: MAX_STRING_CHARS - 1] + "…"


def _safe_code(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if (
        not text
        or len(text) > 128
        or not text.isascii()
        or any(not (character.isalnum() or character in "._:/-") for character in text)
    ):
        return None
    return text


def _source_error(
    source: str, stage: str, error: Exception, schedule_id: str | None = None
) -> Mapping[str, str]:
    record = {
        "source": source,
        "stage": stage,
        "error_type": _safe_code(type(error).__name__) or "Exception",
    }
    if schedule_id is not None:
        record["schedule_id"] = schedule_id[:MAX_STRING_CHARS]
    return record


def _record_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise TypeError("campaign diagnostic source row is not a mapping")


def _timestamp(value: object, tracker: dict[str, int]) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value: object) -> int | float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, float):
        return value if value >= 0 and math.isfinite(value) else None
    return None


def _without_none(value: Mapping[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item is not None}


def _lead_category(kind: str) -> str:
    if kind.startswith(("recon_", "target_reobservation_")):
        return "recon"
    if "handoff" in kind:
        return "handoff"
    if "approved" in kind or "approval" in kind:
        return "approval"
    if "terminal" in kind or kind.startswith("campaign_cleanup"):
        return "terminal"
    if kind.startswith("preflight_"):
        return "prepare"
    return "lead"


def _scheduler_category(event_type: str) -> str:
    if any(part in event_type for part in ("attempt", "claim", "expired", "settled")):
        return "worker"
    return "scheduler"


def _bounded_jsonl(
    rows: Sequence[Mapping[str, object]], *, max_rows: int, max_bytes: int
) -> tuple[bytes, Mapping[str, int]]:
    output = bytearray()
    retained = 0
    for row in rows:
        if retained >= max_rows:
            break
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(output) + len(encoded) > max_bytes:
            break
        output.extend(encoded)
        retained += 1
    return bytes(output), {
        "input": len(rows),
        "retained": retained,
        "dropped": len(rows) - retained,
    }


def _bounded_jsonl_recent(
    rows: Sequence[Mapping[str, object]], *, max_rows: int, max_bytes: int
) -> tuple[bytes, Mapping[str, int]]:
    encoded_rows: list[bytes] = []
    retained_bytes = 0
    for row in reversed(rows[-max_rows:]):
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if retained_bytes + len(encoded) > max_bytes:
            break
        encoded_rows.append(encoded)
        retained_bytes += len(encoded)
    retained = len(encoded_rows)
    return b"".join(reversed(encoded_rows)), {
        "input": len(rows),
        "retained": retained,
        "dropped": len(rows) - retained,
    }


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _bounded_document_rows(
    document: dict[str, object], key: str, max_bytes: int
) -> tuple[bytes, int]:
    rows = document.get(key)
    if not isinstance(rows, list):
        raise RuntimeError(f"campaign diagnostics {key} projection is not a list")
    low = 0
    high = len(rows)
    best = b""
    while low <= high:
        retained = (low + high) // 2
        candidate = _json_bytes({**document, key: rows[:retained]})
        if len(candidate) <= max_bytes:
            best = candidate
            low = retained + 1
        else:
            high = retained - 1
    if not best:
        raise RuntimeError(f"campaign diagnostics {key} metadata exceeded its byte cap")
    document[key] = rows[:high]
    return best, high


def _manifest_bytes(manifest: dict[str, object], members: Mapping[str, bytes]) -> bytes:
    previous = -1
    for _ in range(4):
        encoded = _json_bytes(manifest)
        total = len(encoded) + sum(len(value) for value in members.values())
        manifest["uncompressed_bytes"] = total
        if total == previous:
            break
        previous = total
    encoded = _json_bytes(manifest)
    if len(encoded) > _MEMBER_CAPS["manifest.json"]:
        raise RuntimeError("campaign diagnostics manifest exceeded its structural byte cap")
    return encoded
