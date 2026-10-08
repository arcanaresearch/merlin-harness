"""Unit tests for the advisory Jev semantic dedup over admitted hypotheses."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from merlin_agents.campaign_service import (
    CampaignService,
    FrontierHypothesis,
    LeadAssessment,
    LeadFollowUp,
)
from merlin_agents.jev import JevClient


def _service() -> CampaignService:
    return CampaignService(None, None, object())  # type: ignore[arg-type]


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


def _hypothesis(assertion: str, surface: str = "chat") -> FrontierHypothesis:
    return FrontierHypothesis(
        surface=surface,
        assertion=assertion,
        oracle="the target leaks the canary",
        novelty_basis="not yet covered",
    )


def _assessment(*assertions: str) -> LeadAssessment:
    return LeadAssessment(
        "summary",
        False,
        followups=tuple(
            LeadFollowUp("profile", "objective", (f"task-{index}",), hypothesis=_hypothesis(text))
            for index, text in enumerate(assertions)
        ),
    )


def _new_ids(assessment: LeadAssessment) -> frozenset[str]:
    return frozenset(
        followup.hypothesis.id
        for followup in assessment.followups
        if followup.hypothesis is not None
    )


def _run(
    service: CampaignService,
    assessment: LeadAssessment,
    new_ids: frozenset[str] | None = None,
) -> LeadAssessment:
    return asyncio.run(
        service._jev_hypothesis_dedup(
            "campaign-1",
            1,
            assessment,
            _new_ids(assessment) if new_ids is None else new_ids,
        )
    )


def _kept_assertions(assessment: LeadAssessment) -> list[str]:
    return [
        followup.hypothesis.assertion
        for followup in assessment.followups
        if followup.hypothesis is not None
    ]


def test_confident_same_keeps_later_hypothesis_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.9)
    result = _run(service, _assessment("leak the canary", "expose the canary value"))
    assert _kept_assertions(result) == ["leak the canary", "expose the canary value"]


def test_boundary_same_keeps_later_hypothesis_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.7)
    result = _run(service, _assessment("leak the canary", "expose the canary value"))
    assert _kept_assertions(result) == ["leak the canary", "expose the canary value"]


def test_below_threshold_keeps_both(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.69)
    assessment = _assessment("leak the canary", "expose the canary value")
    result = _run(service, assessment)
    assert _kept_assertions(result) == _kept_assertions(assessment)


def test_transport_failure_keeps_both(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, None)
    assessment = _assessment("leak the canary", "expose the canary value")
    result = _run(service, assessment)
    assert _kept_assertions(result) == _kept_assertions(assessment)


def test_unavailable_client_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    service._jev_client = None
    service._jev_client_checked = True
    payloads = _stub_jev_post(monkeypatch, 0.9)
    assessment = _assessment("leak the canary", "expose the canary value")
    result = _run(service, assessment)
    assert _kept_assertions(result) == _kept_assertions(assessment)
    assert payloads == []


def test_cap_skips_jev_above_twelve_new(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.9)
    assessment = _assessment(*(f"assertion {index}" for index in range(13)))
    result = _run(service, assessment)
    assert _kept_assertions(result) == _kept_assertions(assessment)
    assert payloads == []


def test_no_new_admissions_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.9)
    assessment = _assessment("leak the canary", "expose the canary value")
    result = _run(service, assessment, new_ids=frozenset())
    assert _kept_assertions(result) == _kept_assertions(assessment)
    assert payloads == []


def test_existing_hypotheses_are_anchors_never_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.95)
    assessment = _assessment("leak the canary", "expose the canary value")
    existing = assessment.followups[0].hypothesis
    assert existing is not None
    new_ids = _new_ids(assessment) - frozenset({existing.id})
    result = _run(service, assessment, new_ids=new_ids)
    assert _kept_assertions(result) == ["leak the canary", "expose the canary value"]


def test_duplicate_is_journaled(monkeypatch: pytest.MonkeyPatch) -> None:
    entries: list[Mapping[str, object]] = []

    class _Store:
        def append_lead(self, campaign_id, wave, entry):  # noqa: ANN001, ANN202
            entries.append(entry)

    service = CampaignService(_Store(), None, object())  # type: ignore[arg-type]
    _attach_client(service)
    _stub_jev_post(monkeypatch, 0.9)
    assessment = _assessment("leak the canary", "expose the canary value")
    _run(service, assessment)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "jev_hypothesis_dedup"
    assert entry["status"] == "advisory_duplicate"
    assert entry["graded_by"] == "jev"
    assert "without changing execution eligibility" in entry["reason"]
    duplicates = entry["duplicates"]
    assert len(duplicates) == 1
    assert duplicates[0]["hypothesis_id"] == assessment.followups[1].hypothesis.id
    assert duplicates[0]["duplicate_of"] == assessment.followups[0].hypothesis.id


def test_question_wording_targets_same_behavior_same_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, 0.5)
    _run(service, _assessment("leak the canary", "expose the canary value"))
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    instructions = str(question["instructions"]).lower()  # type: ignore[index]
    assert "same behavior" in instructions
    assert "same surface" in instructions
    assert question["type"] == "noul"  # type: ignore[index]
