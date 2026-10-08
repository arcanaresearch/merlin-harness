"""Unit tests for the advisory Jev cross-check of probe self-reports."""

from __future__ import annotations

import asyncio

import pytest
from merlin_agents.capability_probe_execution import apply_jev_probe_cross_check
from merlin_agents.capability_probes import (
    CapabilityProbe,
    CapabilityProbeKind,
    CapabilitySupport,
    ProbeObservation,
    ProbeTransport,
)
from merlin_agents.jev import JevClient


def _probe() -> CapabilityProbe:
    return CapabilityProbe(
        id="capability/upload-document",
        kind=CapabilityProbeKind.UPLOAD_DOCUMENT,
        support=CapabilitySupport.UNKNOWN,
        required_transport=ProbeTransport.DOCUMENT_UPLOAD,
        instruction="Upload a small plain-text document and ask the target to repeat the nonce.",
    )


def _observation(*, accepted: bool | None, gracefully_rejected: bool = False) -> ProbeObservation:
    return ProbeObservation(
        transport_available=True,
        accepted=accepted,
        gracefully_rejected=gracefully_rejected,
        summary="worker self-report summary",
        evidence_ids=("merlin-result://s/w/a#target-call=call-1",),
    )


_COMMITTED = {
    "target_call_documents": {"call-1": {"reply": "I cannot accept file uploads, sorry."}},
}


def _stub_jev_post(
    monkeypatch: pytest.MonkeyPatch,
    *,
    confirm: float | None,
    deny: float | None,
) -> None:
    """Patch the Jev transport; a None probability simulates a transport failure."""

    def fake_post(url, token, payload, timeout_seconds):  # noqa: ANN001, ANN202
        instructions = payload["questions"]["question"]["instructions"]
        probability = confirm if "explicitly confirm" in instructions else deny
        if probability is None:
            raise TimeoutError("jev unavailable")
        return {
            "answers": {"question": {"noul": probability}},
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)


def _run(
    observation: ProbeObservation,
    *,
    client: JevClient | None,
    committed=_COMMITTED,  # noqa: ANN001, ANN202
) -> ProbeObservation:
    return asyncio.run(
        apply_jev_probe_cross_check(
            _probe(),
            observation,
            committed,
            client=client,
            campaign_id="campaign-1",
        )
    )


def test_confident_denial_disputes_reported_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_jev_post(monkeypatch, confirm=0.1, deny=0.9)

    disputed = _run(_observation(accepted=True), client=JevClient("test-token"))

    assert disputed.accepted is None
    assert disputed.gracefully_rejected is False
    assert disputed.evidence_ids == _observation(accepted=True).evidence_ids
    (uncertainty,) = disputed.evaluation_uncertainties
    assert uncertainty["source"] == "capability_probe.jev_cross_check"
    assert uncertainty["kind"] == "evaluation_uncertain"
    assert uncertainty["graded_by"] == "jev"
    assert uncertainty["confidence"] == pytest.approx(0.9)
    assert uncertainty["reported_accepted"] is True


def test_confident_non_confirmation_disputes_reported_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_jev_post(monkeypatch, confirm=0.1, deny=0.5)

    disputed = _run(_observation(accepted=True), client=JevClient("test-token"))

    assert disputed.accepted is None
    (uncertainty,) = disputed.evaluation_uncertainties
    assert uncertainty["confidence"] == pytest.approx(0.9)


def test_confident_confirmation_disputes_but_never_upgrades_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_jev_post(monkeypatch, confirm=0.9, deny=0.05)

    disputed = _run(
        _observation(accepted=False, gracefully_rejected=True), client=JevClient("test-token")
    )

    assert disputed.accepted is None
    assert disputed.gracefully_rejected is False
    (uncertainty,) = disputed.evaluation_uncertainties
    assert uncertainty["reported_accepted"] is False
    assert uncertainty["reported_gracefully_rejected"] is True


def test_corroborating_verdict_keeps_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_jev_post(monkeypatch, confirm=0.95, deny=0.05)
    observation = _observation(accepted=True)

    assert _run(observation, client=JevClient("test-token")) == observation


def test_uncertain_verdict_keeps_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_jev_post(monkeypatch, confirm=0.5, deny=0.5)
    observation = _observation(accepted=True)

    assert _run(observation, client=JevClient("test-token")) == observation


def test_transport_failure_keeps_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_jev_post(monkeypatch, confirm=None, deny=None)
    observation = _observation(accepted=True)

    assert _run(observation, client=JevClient("test-token")) == observation


def test_no_client_keeps_observation() -> None:
    observation = _observation(accepted=True)

    assert _run(observation, client=None) is observation


def test_missing_exchange_text_keeps_observation() -> None:
    observation = _observation(accepted=True)

    assert _run(observation, client=JevClient("test-token"), committed=None) is observation
    assert _run(observation, client=JevClient("test-token"), committed={}) is observation


def test_ambiguous_self_report_is_not_cross_checked() -> None:
    observation = _observation(accepted=None)

    assert _run(observation, client=JevClient("test-token")) is observation
