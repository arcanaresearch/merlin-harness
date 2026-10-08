"""Unit tests for the advisory Jev severity scoring on finding-draft intake."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from merlin_agents.durable_service import DurableAgentService
from merlin_agents.jev import JevClient
from merlin_agents.specialists import AssignmentBudget, WorkerAssignment
from test_durable_service import Provider, Store, Tools


def _stub_jev_post(
    monkeypatch: pytest.MonkeyPatch,
    score: float | None,
    confidence: float = 0.9,
) -> list[Mapping[str, object]]:
    """Patch the Jev transport; a None score simulates a transport failure."""
    payloads: list[Mapping[str, object]] = []

    def fake_post(url, token, payload, timeout_seconds):  # noqa: ANN001, ANN202
        payloads.append(payload)
        if score is None:
            raise TimeoutError("jev unavailable")
        return {
            "answers": {"question": {"score": score, "confidence": confidence}},
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)
    return payloads


class _Sink:
    def __init__(self) -> None:
        self.created = []

    async def create(self, session_id, draft):  # noqa: ANN001, ANN202
        self.created.append((session_id, draft))


def _assignment(**overrides) -> WorkerAssignment:  # noqa: ANN003
    fields = {
        "id": "task",
        "objective": "Assess target",
        "profile_id": "recon",
        "target_profile_id": "reviewed-target",
        "technique_ids": ("technique/one",),
        "guidance_ids": (),
        "tool_ids": (),
        "skill_ids": (),
        "provider_id": "test",
        "authority_ids": ("session",),
        "budget": AssignmentBudget(3, 10, 30),
        "input_schema": '{"type":"object"}',
        "output_schema": '{"type":"object"}',
    }
    return WorkerAssignment(**{**fields, **overrides})


async def _service_with_live(*, with_client: bool = True):
    store = Store()
    sink = _Sink()
    service = DurableAgentService(Provider(), Tools(), store, model="test", finding_sink=sink)
    if with_client:
        service._jev_client = JevClient("test-token")  # noqa: SLF001
        service._jev_client_checked = True  # noqa: SLF001
    assignment = _assignment()
    live = await service.submit(
        "session-1",
        (assignment,),
        max_workers=1,
        start_immediately=False,
        minimum_workers=1,
    )
    return store, sink, service, live, assignment


async def _persist_worker_draft(service, live, severity: str = "medium") -> None:
    lease = SimpleNamespace(node_id="worker:task", id="attempt-1")
    await service._persist_finding_drafts(  # noqa: SLF001
        live,
        lease,
        {
            "finding_drafts": [
                {
                    "title": "Worker finding",
                    "severity": severity,
                    "summary": "Observed behavior.",
                    "reproduction": "Replay the exchange.",
                    "remediation": "Enforce the boundary.",
                }
            ]
        },
        "sha256:test",
    )


async def test_adaptive_receipt_uses_jev_severity(monkeypatch: pytest.MonkeyPatch) -> None:
    _, sink, service, live, assignment = await _service_with_live()
    _stub_jev_post(monkeypatch, 4.0)
    await service._persist_adaptive_receipt_finding(  # noqa: SLF001
        live, assignment, {"receipt_digest": "sha256:receipt"}
    )
    assert len(sink.created) == 1
    assert sink.created[0][1].severity == "critical"


async def test_adaptive_receipt_rounds_to_nearest_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sink, service, live, assignment = await _service_with_live()
    _stub_jev_post(monkeypatch, 2.6)
    await service._persist_adaptive_receipt_finding(  # noqa: SLF001
        live, assignment, {"receipt_digest": "sha256:receipt"}
    )
    assert sink.created[0][1].severity == "high"


async def test_adaptive_receipt_transport_failure_keeps_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sink, service, live, assignment = await _service_with_live()
    _stub_jev_post(monkeypatch, None)
    await service._persist_adaptive_receipt_finding(  # noqa: SLF001
        live, assignment, {"receipt_digest": "sha256:receipt"}
    )
    assert sink.created[0][1].severity == "medium"


async def test_worker_severity_dispute_is_journaled_but_worker_value_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sink, service, live, _ = await _service_with_live()
    _stub_jev_post(monkeypatch, 0.0, 0.9)
    await _persist_worker_draft(service, live, severity="medium")
    assert sink.created[0][1].severity == "medium"
    disputes = [
        payload
        for _, event_type, payload in store.lifecycle_events
        if event_type == "severity_dispute"
    ]
    assert len(disputes) == 1
    assert disputes[0]["graded_by"] == "jev"
    assert disputes[0]["worker_severity"] == "medium"
    assert disputes[0]["jev_severity"] == "informational"


async def test_one_level_gap_journals_no_dispute(monkeypatch: pytest.MonkeyPatch) -> None:
    store, sink, service, live, _ = await _service_with_live()
    _stub_jev_post(monkeypatch, 1.0, 0.95)
    await _persist_worker_draft(service, live, severity="medium")
    assert sink.created[0][1].severity == "medium"
    assert "severity_dispute" not in {event_type for _, event_type, _ in store.lifecycle_events}


async def test_low_confidence_gap_journals_no_dispute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sink, service, live, _ = await _service_with_live()
    _stub_jev_post(monkeypatch, 0.0, 0.79)
    await _persist_worker_draft(service, live, severity="medium")
    assert sink.created[0][1].severity == "medium"
    assert "severity_dispute" not in {event_type for _, event_type, _ in store.lifecycle_events}


async def test_unavailable_client_never_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sink, service, live, assignment = await _service_with_live(with_client=False)
    payloads = _stub_jev_post(monkeypatch, 4.0)
    await _persist_worker_draft(service, live, severity="medium")
    await service._persist_adaptive_receipt_finding(  # noqa: SLF001
        live, assignment, {"receipt_digest": "sha256:receipt"}
    )
    assert payloads == []
    assert [draft.severity for _, draft in sink.created] == ["medium", "medium"]
    assert "severity_dispute" not in {event_type for _, event_type, _ in store.lifecycle_events}


async def test_question_wording_targets_severity(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, service, live, _ = await _service_with_live()
    payloads = _stub_jev_post(monkeypatch, 2.0)
    await _persist_worker_draft(service, live)
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    instructions = str(question["instructions"]).lower()  # type: ignore[index]
    assert "evidence" in instructions
    assert "severity" in instructions
    assert question["type"] == "score"  # type: ignore[index]
    assert question["levels"] == [  # type: ignore[index]
        "informational",
        "low",
        "medium",
        "high",
        "critical",
    ]
