from __future__ import annotations

import hashlib
import logging
import socket

from merlin_agents.telemetry import (
    TelemetrySettings,
    configure_telemetry,
    emit_target_exchange,
    shutdown_telemetry,
)
from opentelemetry.sdk._logs.export import LogExportResult


class FakeLogExporter:
    def __init__(self, *, fail: bool = False) -> None:
        self.records = []
        self.fail = fail

    def export(self, batch):
        if self.fail:
            raise RuntimeError("export boom")
        self.records.extend(batch)
        return LogExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _configure(exporter: FakeLogExporter) -> None:
    configure_telemetry(
        TelemetrySettings(
            enabled=True,
            endpoint="https://otel.example/v1/logs",
            client="client-a",
        ),
        exporter=exporter,
    )


def _emit_pair() -> None:
    emit_target_exchange(
        call_id="call-1",
        tool_name="target_request",
        attempt_content='{"input": "probe one"}',
        reply_content="target reply body",
        attempt_sha256=hashlib.sha256(b"attempt").hexdigest(),
        campaign_id="campaign/abc",
        session_id="session-1",
        task_id="campaign/abc/task/1",
        target_id="target-1",
        target_endpoint="https://target.example/api",
        technique_ids=("T1", "T2"),
        is_error=False,
        status_code=200,
        latency_ms=42,
    )


def test_disabled_config_is_a_noop() -> None:
    shutdown_telemetry()
    configure_telemetry(TelemetrySettings())
    _emit_pair()
    configure_telemetry(TelemetrySettings(enabled=True))  # enabled but no endpoint
    _emit_pair()


def test_attempt_and_reply_records_carry_required_fields() -> None:
    exporter = FakeLogExporter()
    _configure(exporter)
    try:
        _emit_pair()
    finally:
        shutdown_telemetry()
    assert [record.log_record.attributes["merlin.event"] for record in exporter.records] == [
        "attempt",
        "reply",
    ]
    attempt, reply = (record.log_record for record in exporter.records)
    assert attempt.body == '{"input": "probe one"}'
    assert attempt.attributes["merlin.call_id"] == "call-1"
    assert attempt.attributes["merlin.client"] == "client-a"
    assert attempt.attributes["merlin.tool_name"] == "target_request"
    assert attempt.attributes["merlin.campaign_id"] == "campaign/abc"
    assert attempt.attributes["merlin.session_id"] == "session-1"
    assert attempt.attributes["merlin.task_id"] == "campaign/abc/task/1"
    assert attempt.attributes["merlin.target_id"] == "target-1"
    assert attempt.attributes["merlin.target_endpoint"] == "https://target.example/api"
    assert list(attempt.attributes["merlin.technique_ids"]) == ["T1", "T2"]
    assert attempt.attributes["merlin.content_sha256"] == hashlib.sha256(b"attempt").hexdigest()
    assert attempt.timestamp > 0

    assert reply.body == "target reply body"
    assert reply.attributes["merlin.content_bytes"] == len(b"target reply body")
    assert (
        reply.attributes["merlin.content_sha256"]
        == hashlib.sha256(b"target reply body").hexdigest()
    )
    assert reply.attributes["merlin.status_code"] == 200
    assert reply.attributes["merlin.latency_ms"] == 42
    assert reply.attributes["merlin.is_error"] is False
    assert reply.attributes["merlin.client"] == "client-a"
    assert "merlin.latency_ms" not in attempt.attributes


def test_client_label_defaults_to_hostname() -> None:
    exporter = FakeLogExporter()
    configure_telemetry(
        TelemetrySettings(enabled=True, endpoint="https://otel.example/v1/logs"),
        exporter=exporter,
    )
    try:
        _emit_pair()
    finally:
        shutdown_telemetry()
    clients = {record.log_record.attributes["merlin.client"] for record in exporter.records}
    assert clients == {socket.gethostname()}


def test_content_excerpt_is_bounded() -> None:
    exporter = FakeLogExporter()
    _configure(exporter)
    try:
        emit_target_exchange(
            call_id="call-2",
            tool_name="target_chat",
            attempt_content="x" * 10_000,
            reply_content="y" * 10_000,
            attempt_sha256="a" * 64,
        )
    finally:
        shutdown_telemetry()
    attempt, reply = (record.log_record for record in exporter.records)
    assert len(attempt.body) == 2_048
    assert len(reply.body) == 2_048
    assert attempt.attributes["merlin.content_bytes"] == 10_000
    assert attempt.attributes["merlin.content_excerpt_truncated"] is True


def test_export_failure_is_swallowed(caplog) -> None:
    exporter = FakeLogExporter(fail=True)
    _configure(exporter)
    try:
        with caplog.at_level(logging.DEBUG, logger="merlin_agents.telemetry"):
            _emit_pair()  # must not raise
    finally:
        shutdown_telemetry()


def test_arm_outcome_and_finding_verdict_records() -> None:
    from merlin_agents.telemetry import emit_arm_outcome, emit_finding_verdict

    exporter = FakeLogExporter()
    _configure(exporter)
    try:
        emit_arm_outcome(
            {
                "campaign_id": "campaign/abc",
                "arm_id": "arm:1",
                "hypothesis_id": "hypothesis:1",
                "technique_attempt_id": "attempt-1",
                "identity": {
                    "technique_id": "case:x",
                    "controller_id": "crescendo",
                    "mutation_id": "mut-1",
                },
                "actual_language": "en",
                "requested_transform_id": "t-req",
                "applied_transform_id": "t-applied",
                "outcome": "confirmed",
                "signal": 0.8,
                "distance_to_success": 0.2,
                "target_calls": 2,
                "model_steps": 3,
                "latency_ms": 1200,
                "model_cost_usd": 0.01,
                "usage": {"total_tokens": 1234},
                "arm_objective": "extract the system prompt",
                "semantic_id": "prompt-extraction",
                "strategy_id": "direct-ask",
                "hypothesis_assertion": "the bot will reveal its instructions",
                "hypothesis_surface": "chat-output",
            }
        )
        emit_finding_verdict(
            campaign_id="campaign/abc",
            finding_id="finding-1",
            technique_id="case:x",
            verdict="partial",
            severity="medium",
            title="Partial instruction leak",
            summary="The bot revealed one rule before refusing.",
        )
    finally:
        shutdown_telemetry()
    outcome, verdict = (record.log_record for record in exporter.records)
    assert outcome.attributes["merlin.event"] == "outcome"
    assert outcome.attributes["merlin.client"] == "client-a"
    assert outcome.attributes["merlin.outcome"] == "confirmed"
    assert outcome.attributes["merlin.controller_id"] == "crescendo"
    assert outcome.attributes["merlin.signal"] == 0.8
    assert outcome.attributes["merlin.distance_to_success"] == 0.2
    assert outcome.attributes["merlin.model_cost_usd"] == 0.01
    assert outcome.attributes["merlin.total_tokens"] == 1234
    assert outcome.attributes["merlin.arm_objective"] == "extract the system prompt"
    assert outcome.attributes["merlin.semantic_id"] == "prompt-extraction"
    assert outcome.attributes["merlin.strategy_id"] == "direct-ask"
    assert (
        outcome.attributes["merlin.hypothesis_assertion"] == "the bot will reveal its instructions"
    )
    assert outcome.attributes["merlin.hypothesis_surface"] == "chat-output"
    # semantic_id wins for the readable technique name
    assert outcome.attributes["merlin.technique_name"] == "Prompt Extraction"
    assert outcome.body == "outcome confirmed · Prompt Extraction"
    assert verdict.attributes["merlin.event"] == "finding_verdict"
    assert verdict.body == "finding partial · medium · Partial instruction leak"
    assert verdict.attributes["merlin.verdict"] == "partial"
    assert verdict.attributes["merlin.finding_id"] == "finding-1"
    assert verdict.attributes["merlin.technique_name"] == "X"
    assert verdict.attributes["merlin.finding_title"] == "Partial instruction leak"
    assert (
        verdict.attributes["merlin.finding_summary"] == "The bot revealed one rule before refusing."
    )
