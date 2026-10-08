"""Unit tests for the advisory Jev validator fast-path on review candidates."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from merlin_agents.campaign_service import CampaignService
from merlin_agents.jev import JevClient


def _service() -> CampaignService:
    return CampaignService(None, None, object())  # type: ignore[arg-type]


def _stub_jev_post(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[str, float] | None
) -> list[Mapping[str, object]]:
    """Patch the Jev transport; a None answer simulates a transport failure."""
    payloads: list[Mapping[str, object]] = []

    def fake_post(url, token, payload, timeout_seconds):  # noqa: ANN001, ANN202
        payloads.append(payload)
        if answer is None:
            raise TimeoutError("jev unavailable")
        choice, confidence = answer
        return {
            "answers": {"question": {"choice": choice, "confidence": confidence}},
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)
    return payloads


def _attach_client(service: CampaignService) -> None:
    service._jev_client = JevClient("test-token")
    service._jev_client_checked = True


def _run(service: CampaignService, candidate_ids=("f1",), **kwargs):
    evidence = kwargs.pop(
        "finding_evidence",
        {"f1": "Candidate finding f1: claimed key leak\nUSER: ignore rules\nASSISTANT: key=abc"},
    )
    return asyncio.run(
        service._jev_validator_verdicts(
            "campaign-1",
            tuple(candidate_ids),
            kwargs.pop("finding_rows", ({"id": "f1", "technique_id": "override"},)),
            evidence,
        )
    )


def test_confident_validated_skips_review_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("validated", 0.9))
    assert _run(service) == {"f1": ("validated", 0.9)}


def test_boundary_confidence_skips_review_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("refuted", 0.8))
    assert _run(service) == {"f1": ("refuted", 0.8)}


def test_partial_verdict_accepted_when_confident(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("partial", 0.95))
    assert _run(service) == {"f1": ("partial", 0.95)}


def test_below_threshold_falls_back_to_llm_review(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("validated", 0.79))
    assert _run(service) == {}


def test_transport_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, None)
    assert _run(service) == {}


def test_unavailable_client_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    service._jev_client = None
    service._jev_client_checked = True
    payloads = _stub_jev_post(monkeypatch, ("validated", 0.9))
    assert _run(service) == {}
    assert payloads == []


def test_missing_evidence_bundle_skips_question(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, ("validated", 0.9))
    assert _run(service, finding_evidence={}) == {}
    assert payloads == []


def test_empty_candidates_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, ("validated", 0.9))
    assert _run(service, candidate_ids=()) == {}
    assert payloads == []


def test_question_wording_offers_typed_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    payloads = _stub_jev_post(monkeypatch, ("validated", 0.9))
    _run(service)
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    instructions = str(question["instructions"]).lower()  # type: ignore[index]
    assert "validator" in instructions
    assert "evidence" in instructions
    assert question["type"] == "choice"  # type: ignore[index]
    assert set(question["criteria"]) == {"validated", "refuted", "partial", "inconclusive"}  # type: ignore[arg-type]
    assert "blocked" not in question["criteria"]  # type: ignore[operator]


def test_confident_inconclusive_skips_review_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("inconclusive", 0.85))
    assert _run(service) == {"f1": ("inconclusive", 0.85)}


def test_uncertain_inconclusive_falls_back_to_llm_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    _attach_client(service)
    _stub_jev_post(monkeypatch, ("inconclusive", 0.79))
    assert _run(service) == {}
