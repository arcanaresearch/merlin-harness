"""Unit tests for the advisory Jev cross-check on Lead coverage_complete claims."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from merlin_agents.campaign_service import CampaignService, LeadAssessment
from merlin_agents.jev import JevClient


@dataclass(frozen=True, slots=True)
class _Cell:
    id: str
    objective: str = "cell objective"
    strategy_id: str = "direct"
    transform_id: str = "baseline"
    language: str = "en"


@dataclass(frozen=True, slots=True)
class _Manifest:
    id: str = "campaign-1"
    objective: str = "campaign objective"
    catalogue: tuple[_Cell, ...] = (_Cell("cell-1"), _Cell("cell-2"))


class _Store:
    def __init__(self) -> None:
        self.lead_rows: list[Mapping[str, object]] = []

    def list_waves(self, _campaign_id: str):
        return (
            {
                "wave": 0,
                "tasks": (
                    {
                        "id": "task-1",
                        "status": "completed",
                        "technique_outcomes": {"cell-1": "confirmed"},
                    },
                ),
            },
        )

    def append_lead(self, _campaign_id: str, _wave: int, payload: Mapping[str, object]) -> None:
        self.lead_rows.append(payload)


def _service() -> CampaignService:
    service = CampaignService(_Store(), None, object())  # type: ignore[arg-type]
    return service


def _stub_jev_post(
    monkeypatch: pytest.MonkeyPatch, probability: float | None
) -> list[Mapping[str, object]]:
    """Patch the Jev transport; a None probability simulates a transport failure."""
    payloads: list[Mapping[str, object]] = []

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


def _run(service: CampaignService, assessment: LeadAssessment | None = None) -> LeadAssessment:
    return asyncio.run(
        service._jev_coverage_cross_check(
            "campaign-1",
            _Manifest(),  # type: ignore[arg-type]
            0,
            assessment or LeadAssessment("lead claims coverage complete", True),
        )
    )


def test_confident_incomplete_vetoes_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.1)
    assert _run(service).coverage_complete is False


def test_boundary_incomplete_vetoes_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.3)
    assert _run(service).coverage_complete is False


def test_veto_is_journaled_with_jev_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.2)
    _run(service)
    overrides = [
        row
        for row in service._store.lead_rows
        if row.get("kind") == "jev_coverage_complete_override"
    ]
    assert len(overrides) == 1
    assert overrides[0]["graded_by"] == "jev"
    assert overrides[0]["jev_probability"] == 0.2


def test_confident_complete_keeps_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.9)
    assert _run(service).coverage_complete is True


def test_uncertain_band_keeps_existing_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.5)
    assert _run(service).coverage_complete is True


def test_transport_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, None)
    assert _run(service).coverage_complete is True


def test_unavailable_client_keeps_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    service._jev_client = None
    service._jev_client_checked = True
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assert _run(service).coverage_complete is True
    assert payloads == []


def test_false_claim_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assessment = LeadAssessment("coverage still open", False)
    assert _run(service, assessment).coverage_complete is False
    assert payloads == []


def test_question_wording_targets_coverage_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.9)
    _run(service)
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    instructions = str(question["instructions"]).lower()  # type: ignore[index]
    assert "admitted" in instructions
    assert "hypothesis" in instructions
    assert question["type"] == "noul"  # type: ignore[index]
    state = str(payloads[0]["state"])
    assert "cell-1" in state
    assert "unsearched" in state


class _OverrideJournalStore(_Store):
    """Store fake whose journal already holds prior same-wave Jev overrides."""

    def __init__(self, prior_overrides: int, *, override_wave: int = 0) -> None:
        super().__init__()
        self._prior = [
            {"wave": override_wave, "payload": {"kind": "jev_coverage_complete_override"}}
            for _ in range(prior_overrides)
        ]

    def list_lead(self, _campaign_id: str):
        return [*self._prior, *({"wave": 0, "payload": row} for row in self.lead_rows)]


def test_consecutive_override_cap_accepts_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three consecutive same-wave vetoes with no newly dispatched work: the
    # cross-check stops overriding so the campaign can conclude, and never
    # pays for another Jev call.
    service = CampaignService(_OverrideJournalStore(3), None, object())  # type: ignore[arg-type]
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assert _run(service).coverage_complete is True
    assert payloads == []
    caps = [
        row for row in service._store.lead_rows if row.get("kind") == "jev_coverage_override_cap"
    ]
    assert len(caps) == 1
    assert caps[0]["consecutive_overrides"] == 3


def test_override_cap_resets_when_a_new_wave_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Overrides from an earlier wave do not count once work advanced.
    service = CampaignService(  # type: ignore[arg-type]
        _OverrideJournalStore(3, override_wave=0), None, object()
    )
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.1)
    assessment = asyncio.run(
        service._jev_coverage_cross_check(
            "campaign-1",
            _Manifest(),  # type: ignore[arg-type]
            1,
            LeadAssessment("lead claims coverage complete", True),
        )
    )
    assert assessment.coverage_complete is False
