"""Unit tests for the advisory Jev assist on low/medium validation candidates."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from merlin_agents.campaign_service import CampaignService
from merlin_agents.jev import JevClient


def _service() -> CampaignService:
    return CampaignService(None, None, object())  # type: ignore[arg-type]


def _stub_jev_post(monkeypatch: pytest.MonkeyPatch, probability: float | None) -> list:
    """Patch the Jev transport; a None probability simulates a transport failure."""
    payloads: list = []

    def fake_post(url, token, payload, timeout_seconds):  # noqa: ANN001, ANN202
        payloads.append(payload)
        if probability is None:
            raise TimeoutError("jev unavailable")
        return {
            "answers": {"question": {"noul": probability}},
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)
    return payloads


def _attach_client(service: CampaignService) -> None:
    service._jev_client = JevClient("test-token")
    service._jev_client_checked = True


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(id="campaign-1", target=SimpleNamespace(id="target-1"))


def _finding(finding_id: str, *, severity: str = "low", creator: str = "task-1") -> dict:
    return {
        "id": finding_id,
        "creator": creator,
        "validation": "unvalidated",
        "target_id": "target-1",
        "severity": severity,
        "technique_id": "override",
        "summary": f"draft {finding_id}",
        "evidence_references": [f"evidence:{finding_id}"],
    }


def _run(
    service: CampaignService,
    findings: tuple[dict, ...],
    verified: tuple[str, ...] = ("f1",),
    source_task_ids: frozenset[str] = frozenset({"task-1"}),
):
    return asyncio.run(
        service._jev_validation_candidate_assist(
            _manifest(), findings, set(source_task_ids), verified
        )
    )


def test_confident_no_drops_picked_low_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.1)
    assert _run(service, (_finding("f1"),)) == ()


def test_boundary_no_drops_picked_low_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.3)
    assert _run(service, (_finding("f1"),)) == ()


def test_confident_yes_admits_omitted_low_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.9)
    findings = (_finding("f2", creator="task-other"),)
    assert _run(service, findings, verified=()) == ("f2",)


def test_boundary_yes_admits_omitted_low_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.7)
    findings = (_finding("f2", creator="task-other"),)
    assert _run(service, findings, verified=()) == ("f2",)


def test_uncertain_band_keeps_deterministic_set(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.5)
    findings = (_finding("f1"), _finding("f2", creator="task-other"))
    assert _run(service, findings) == ("f1",)


def test_transport_failure_keeps_deterministic_set(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, None)
    findings = (_finding("f1"), _finding("f2", creator="task-other"))
    assert _run(service, findings) == ("f1",)


def test_unavailable_client_keeps_deterministic_set(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    service._jev_client = None
    service._jev_client_checked = True
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assert _run(service, (_finding("f1"),)) == ("f1",)
    assert payloads == []


def test_high_severity_never_assisted(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.1)
    findings = (_finding("f1", severity="high"), _finding("f2", severity="critical"))
    assert _run(service, findings, verified=("f1", "f2")) == ("f1", "f2")
    assert payloads == []


def test_omitted_without_evidence_never_assisted(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.9)
    finding = _finding("f2", creator="task-other")
    finding["evidence_references"] = []
    assert _run(service, (finding,), verified=()) == ()
    assert payloads == []


def test_question_wording_targets_validation_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.5)
    _run(service, (_finding("f1"),))
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]
    instructions = str(question["instructions"]).lower()
    assert "independent validation wave" in instructions
    assert "evidence" in instructions
    assert question["type"] == "noul"
    state = str(payloads[0]["state"])
    assert "draft f1" in state
    assert "evidence_digests" in state
