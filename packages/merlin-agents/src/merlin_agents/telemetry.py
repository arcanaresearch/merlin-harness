"""Optional OpenTelemetry log telemetry for target exchange attempts and replies.

Off by default: unless ``configure_telemetry`` runs with an enabled, endpoint-
configured settings object, ``emit_target_exchange`` is a zero-overhead no-op.
Export failures are logged locally at debug and dropped — telemetry must never
raise into the worker path.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 2_048

_otel_logger: object | None = None
_client: str = ""


def _default_client() -> str:
    """Auto-identify this installation by machine name when unconfigured."""
    try:
        return socket.gethostname().strip() or "unknown-host"
    except OSError:
        return "unknown-host"


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    enabled: bool = False
    endpoint: str | None = None
    token: str | None = None
    service_name: str = "merlin-harness"
    client: str | None = None


def configure_telemetry(settings: TelemetrySettings, *, exporter: object | None = None) -> None:
    """Wire the module-level emitter once from daemon/runtime startup config.

    ``exporter`` injects a fake OTLP log exporter for tests and self-checks; an
    injected exporter uses a synchronous processor so emitted records are
    exported deterministically.
    """
    global _otel_logger, _client
    _otel_logger = None
    _client = settings.client or _default_client()
    if not settings.enabled or not settings.endpoint:
        return
    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            BatchLogRecordProcessor,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        _logger.debug("opentelemetry SDK unavailable; target exchange telemetry disabled")
        return
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        headers = {"Authorization": f"Bearer {settings.token}"} if settings.token else {}
        exporter = OTLPLogExporter(endpoint=settings.endpoint, headers=headers)
        processor = BatchLogRecordProcessor(exporter)
    else:
        processor = SimpleLogRecordProcessor(exporter)  # type: ignore[arg-type]
    provider = LoggerProvider(resource=Resource.create({"service.name": settings.service_name}))
    provider.add_log_record_processor(processor)
    _otel_logger = provider.get_logger("merlin.target_exchange")


def shutdown_telemetry() -> None:
    """Flush and tear down the module-level emitter (daemon shutdown/tests)."""
    global _otel_logger, _client
    logger, _otel_logger, _client = _otel_logger, None, ""
    if logger is None:
        return
    try:
        provider = getattr(logger, "_logger_provider", None)
        if provider is not None:
            provider.shutdown()
    except Exception:  # noqa: BLE001 - telemetry teardown must never raise
        _logger.debug("telemetry shutdown failed", exc_info=True)


def emit_target_exchange(
    *,
    call_id: str,
    tool_name: str,
    attempt_content: str,
    reply_content: str,
    attempt_sha256: str,
    campaign_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    target_id: str | None = None,
    target_endpoint: str | None = None,
    technique_ids: Sequence[str] = (),
    is_error: bool = False,
    status_code: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Emit one attempt record and one reply record for a target exchange.

    Digests and byte counts cover the full content so analysts can join against
    durable receipts (``target-call:<call_id>``); only bounded excerpts ship.
    """
    logger = _otel_logger
    if logger is None:
        return
    try:
        shared = {
            "merlin.call_id": call_id,
            "merlin.tool_name": tool_name,
            "merlin.client": _client,
            "merlin.campaign_id": campaign_id or "",
            "merlin.session_id": session_id or "",
            "merlin.task_id": task_id or "",
            "merlin.target_id": target_id or "",
            "merlin.target_endpoint": target_endpoint or "",
            "merlin.technique_ids": [str(item) for item in technique_ids],
        }
        _emit(
            logger,
            kind="attempt",
            content=attempt_content,
            content_sha256=attempt_sha256,
            attributes=shared,
        )
        _emit(
            logger,
            kind="reply",
            content=reply_content,
            content_sha256=hashlib.sha256(reply_content.encode("utf-8")).hexdigest(),
            attributes={
                **shared,
                "merlin.is_error": bool(is_error),
                **({"merlin.status_code": int(status_code)} if status_code is not None else {}),
                **({"merlin.latency_ms": int(latency_ms)} if latency_ms is not None else {}),
            },
        )
    except Exception:  # noqa: BLE001 - export failure must never reach the worker
        _logger.debug("target exchange telemetry export failed", exc_info=True)


def _emit(
    logger: object,
    *,
    kind: str,
    content: str,
    content_sha256: str,
    attributes: Mapping[str, object],
) -> None:
    from opentelemetry._logs import LogRecord
    from opentelemetry._logs.severity import SeverityNumber

    encoded = content.encode("utf-8")
    is_error_reply = kind == "reply" and attributes.get("merlin.is_error")
    record = LogRecord(
        timestamp=time.time_ns(),
        severity_number=SeverityNumber.ERROR if is_error_reply else SeverityNumber.INFO,
        body=content[:_EXCERPT_CHARS],
        attributes={
            "merlin.event": kind,
            **attributes,
            "merlin.content_bytes": len(encoded),
            "merlin.content_sha256": content_sha256,
            "merlin.content_excerpt_truncated": len(content) > _EXCERPT_CHARS,
        },
    )
    logger.emit(record)  # type: ignore[attr-defined]


def _human_label(value: object) -> str:
    """Humanize a slug id (``recursive-expansion`` → ``Recursive Expansion``)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    raw = raw.split(":", 1)[-1] if ":" in raw else raw
    return re.sub(r"[-_]+", " ", raw).strip().title()


def _emit_event(
    logger: object, *, kind: str, attributes: Mapping[str, object], body: str = ""
) -> None:
    """Emit a metadata-only outcome record (short human-readable body)."""
    from opentelemetry._logs import LogRecord
    from opentelemetry._logs.severity import SeverityNumber

    record = LogRecord(
        timestamp=time.time_ns(),
        severity_number=SeverityNumber.INFO,
        body=body[:256],
        attributes={"merlin.event": kind, "merlin.client": _client, **attributes},
    )
    logger.emit(record)  # type: ignore[attr-defined]


def emit_arm_outcome(outcome: Mapping[str, object]) -> None:
    """Emit the terminal reward signal for one arm/hypothesis attempt.

    Deterministic metadata only — no model calls, no content. ``outcome`` is
    the same mapping persisted to ``campaign_arm_outcomes``.
    """
    logger = _otel_logger
    if logger is None:
        return
    try:
        identity = outcome.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        usage = outcome.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        attributes: dict[str, object] = {
            "merlin.campaign_id": str(outcome.get("campaign_id") or ""),
            "merlin.arm_id": str(outcome.get("arm_id") or ""),
            "merlin.hypothesis_id": str(outcome.get("hypothesis_id") or ""),
            "merlin.technique_attempt_id": str(outcome.get("technique_attempt_id") or ""),
            "merlin.technique_id": str(identity.get("technique_id") or ""),
            "merlin.controller_id": str(identity.get("controller_id") or ""),
            "merlin.mutation_id": str(identity.get("mutation_id") or ""),
            "merlin.language": str(outcome.get("actual_language") or ""),
            "merlin.requested_transform_id": str(outcome.get("requested_transform_id") or ""),
            "merlin.applied_transform_id": str(outcome.get("applied_transform_id") or ""),
            "merlin.outcome": str(outcome.get("outcome") or ""),
            "merlin.target_calls": int(outcome.get("target_calls") or 0),
            "merlin.model_steps": int(outcome.get("model_steps") or 0),
            "merlin.latency_ms": int(outcome.get("latency_ms") or 0),
            "merlin.model_cost_usd": float(outcome.get("model_cost_usd") or 0.0),
            "merlin.total_tokens": int(usage.get("total_tokens") or 0),
        }
        if outcome.get("signal") is not None:
            attributes["merlin.signal"] = float(outcome["signal"])  # type: ignore[arg-type]
        if outcome.get("distance_to_success") is not None:
            attributes["merlin.distance_to_success"] = float(
                outcome["distance_to_success"]  # type: ignore[arg-type]
            )
        # Semantic context inline, so records are meaningful without a DB join.
        technique_name = _human_label(outcome.get("semantic_id") or identity.get("technique_id"))
        if technique_name:
            attributes["merlin.technique_name"] = technique_name[:256]
        for key, attribute in (
            ("arm_objective", "merlin.arm_objective"),
            ("semantic_id", "merlin.semantic_id"),
            ("strategy_id", "merlin.strategy_id"),
            ("hypothesis_assertion", "merlin.hypothesis_assertion"),
            ("hypothesis_surface", "merlin.hypothesis_surface"),
        ):
            text = str(outcome.get(key) or "").strip()
            if text:
                attributes[attribute] = text[:512]
        _emit_event(
            logger,
            kind="outcome",
            attributes=attributes,
            body=(
                f"outcome {attributes['merlin.outcome']}"
                + (f" · {technique_name}" if technique_name else "")
            ),
        )
    except Exception:  # noqa: BLE001 - export failure must never reach the worker
        _logger.debug("arm outcome telemetry export failed", exc_info=True)


def emit_judgment(
    *,
    campaign_id: str,
    task_id: str,
    technique_ids: Sequence[str],
    verdict: str,
    confidence: float,
    graded_by: str,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Emit one advisory refusal-judgment record (semantic grading seam)."""
    logger = _otel_logger
    if logger is None:
        return
    try:
        _emit_event(
            logger,
            kind="judgment",
            body=f"judgment {verdict} · {graded_by} · {float(confidence):.2f}",
            attributes={
                "merlin.campaign_id": campaign_id,
                "merlin.task_id": task_id,
                "merlin.technique_ids": ",".join(technique_ids)[:512],
                "merlin.judgment_verdict": verdict,
                "merlin.judgment_confidence": float(confidence),
                "merlin.graded_by": graded_by,
                "merlin.latency_ms": int(latency_ms),
                "merlin.input_tokens": int(input_tokens),
                "merlin.output_tokens": int(output_tokens),
            },
        )
    except Exception:  # noqa: BLE001
        _logger.debug("judgment telemetry export failed", exc_info=True)


def emit_finding_verdict(
    *,
    campaign_id: str,
    finding_id: str,
    technique_id: str,
    verdict: str,
    severity: str,
    title: str = "",
    summary: str = "",
) -> None:
    """Emit the independent-review verdict for one finding (the honest reward)."""
    logger = _otel_logger
    if logger is None:
        return
    try:
        technique_name = _human_label(technique_id)
        _emit_event(
            logger,
            kind="finding_verdict",
            body=(
                f"finding {verdict} · {severity}" + (f" · {title.strip()}" if title.strip() else "")
            ),
            attributes={
                "merlin.campaign_id": campaign_id,
                "merlin.finding_id": finding_id,
                "merlin.technique_id": technique_id,
                "merlin.technique_name": technique_name,
                "merlin.verdict": verdict,
                "merlin.severity": severity,
                "merlin.finding_title": title.strip()[:256],
                "merlin.finding_summary": summary.strip()[:512],
            },
        )
    except Exception:  # noqa: BLE001
        _logger.debug("finding verdict telemetry export failed", exc_info=True)
