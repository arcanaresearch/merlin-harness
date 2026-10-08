"""Unit tests for the telemetry-only Jev novelty shadow mode."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

import pytest
from merlin_agents import discovery
from merlin_agents.discovery import (
    check_novelty,
    jev_shadow_novelty,
    schedule_novelty_shadow,
)
from merlin_agents.jev import JevClient


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
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)
    return payloads


def _capture_judgments(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "merlin_agents.discovery.emit_judgment", lambda **kwargs: emitted.append(kwargs)
    )
    return emitted


def _corpus() -> tuple[str, ...]:
    return (
        "Extract the admin API token from the chat export",
        "Probe the logout endpoint for session fixation",
        "Enumerate tenant identifiers via the search filter",
    )


def test_shadow_records_jev_vs_jaccard_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _stub_jev_post(monkeypatch, 0.9)
    emitted = _capture_judgments(monkeypatch)
    candidate = "Extract the admin API token from the chat export"
    asyncio.run(jev_shadow_novelty(candidate, _corpus(), client=JevClient("test-token")))
    assert len(payloads) == 1
    state = str(payloads[0]["state"])
    assert candidate in state
    assert "1. " in state
    assert len(emitted) == 1
    record = emitted[0]
    assert record["verdict"] == "jev_same_vs_jaccard_duplicate"
    assert record["confidence"] == pytest.approx(0.9)
    assert record["graded_by"] == "jev-shadow"
    assert record["input_tokens"] == 5
    assert record["output_tokens"] == 2


def test_shadow_records_different_with_inverted_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_jev_post(monkeypatch, 0.2)
    emitted = _capture_judgments(monkeypatch)
    candidate = "Steal the signing key through the debug console"
    asyncio.run(jev_shadow_novelty(candidate, _corpus(), client=JevClient("test-token")))
    jaccard = check_novelty(candidate, _corpus())
    assert emitted[0]["verdict"] == f"jev_different_vs_jaccard_{jaccard.verdict}"
    assert emitted[0]["confidence"] == pytest.approx(0.8)


def test_shadow_skips_when_no_neighbors(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _stub_jev_post(monkeypatch, 0.9)
    emitted = _capture_judgments(monkeypatch)
    asyncio.run(jev_shadow_novelty("anything", (), client=JevClient("test-token")))
    assert payloads == []
    assert emitted == []


def test_shadow_transport_failure_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_jev_post(monkeypatch, None)
    emitted = _capture_judgments(monkeypatch)
    asyncio.run(jev_shadow_novelty("Extract the admin API token", _corpus(), client=JevClient("t")))
    assert emitted == []


def test_shadow_question_bounds_neighbors_to_five(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _stub_jev_post(monkeypatch, 0.9)
    _capture_judgments(monkeypatch)
    corpus = tuple(f"Extract the admin API token variant {index}" for index in range(9))
    asyncio.run(jev_shadow_novelty("Extract the admin API token", corpus, client=JevClient("t")))
    state = str(payloads[0]["state"])
    assert "5. " in state
    assert "6. " not in state


def test_schedule_is_noop_without_client_or_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("merlin_agents.discovery._shadow_client_checked", True)
    monkeypatch.setattr("merlin_agents.discovery._shadow_client", None)
    schedule_novelty_shadow("objective", _corpus())
    schedule_novelty_shadow("objective", _corpus(), client=JevClient("test-token"))


def test_schedule_runs_on_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _stub_jev_post(monkeypatch, 0.7)
    emitted = _capture_judgments(monkeypatch)

    async def run() -> None:
        schedule_novelty_shadow(
            "Extract the admin API token from the chat export",
            _corpus(),
            client=JevClient("test-token"),
        )
        await asyncio.gather(
            *(task for task in asyncio.all_tasks() if task is not asyncio.current_task())
        )

    asyncio.run(run())
    assert len(payloads) == 1
    assert len(emitted) == 1


def test_lazy_client_disabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("merlin_agents.discovery._shadow_client_checked", False)
    monkeypatch.setattr("merlin_agents.discovery._shadow_client", None)
    try:
        import merlin_config
    except ImportError:
        pytest.skip("merlin_config unavailable")
    config = merlin_config.load_config()
    judgments = replace(config.judgments, jev_token=None, jev_decisions_enabled=True)
    monkeypatch.setattr(merlin_config, "load_config", lambda: replace(config, judgments=judgments))
    assert discovery._jev_shadow_client() is None
