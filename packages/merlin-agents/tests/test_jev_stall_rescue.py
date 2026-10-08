"""Unit tests for the advisory Jev rescue of lexical no-progress stall declarations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from merlin_agents.jev import JevClient
from merlin_agents.runtime import WorkerRuntime


def _runtime() -> WorkerRuntime:
    return WorkerRuntime(None, None)  # type: ignore[arg-type]


def _handoff() -> SimpleNamespace:
    return SimpleNamespace(
        assignment=SimpleNamespace(
            id="campaign/c1/worker/w1",
            technique_ids=("catalogue/injection/sql",),
        )
    )


def _history(first: str = "no rows found", second: str = "no rows found"):
    return [
        {"target_request": "fp", "target_progress": "p", "target_excerpt": first},
        {"target_request": "fp", "target_progress": "p", "target_excerpt": second},
    ]


def _stub_jev_post(
    monkeypatch: pytest.MonkeyPatch, noul: float | None
) -> list[Mapping[str, object]]:
    """Patch the Jev transport; a None probability simulates a transport failure."""
    payloads: list[Mapping[str, object]] = []

    def fake_post(url, token, payload, timeout_seconds):  # noqa: ANN001, ANN202
        payloads.append(payload)
        if noul is None:
            raise TimeoutError("jev unavailable")
        return {
            "answers": {"question": {"noul": noul}},
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }

    monkeypatch.setattr("merlin_agents.jev.jev_post", fake_post)
    return payloads


def _attach_client(runtime: WorkerRuntime) -> None:
    runtime._jev_client = JevClient("test-token")
    runtime._jev_client_checked = True


def _run(runtime: WorkerRuntime, history=None) -> bool:
    entries = _history() if history is None else history
    return asyncio.run(runtime._jev_rescues_stall(_handoff(), entries))


def test_confident_different_rescues_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    _stub_jev_post(monkeypatch, 0.1)
    assert _run(runtime) is True


def test_boundary_probability_rescues_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    _stub_jev_post(monkeypatch, 0.3)
    assert _run(runtime) is True


def test_uncertain_or_same_keeps_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    _stub_jev_post(monkeypatch, 0.5)
    assert _run(runtime) is False
    _stub_jev_post(monkeypatch, 0.9)
    assert _run(runtime) is False


def test_transport_failure_keeps_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    _stub_jev_post(monkeypatch, None)
    assert _run(runtime) is False


def test_unavailable_client_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    runtime._jev_client = None
    runtime._jev_client_checked = True
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assert _run(runtime) is False
    assert payloads == []


def test_missing_excerpt_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    payloads = _stub_jev_post(monkeypatch, 0.1)
    assert _run(runtime, history=[{"target_request": "fp", "target_progress": "p"}]) is False
    assert payloads == []


def test_state_carries_both_excerpts_and_noul_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    _attach_client(runtime)
    payloads = _stub_jev_post(monkeypatch, 0.1)
    _run(runtime, history=_history("no rows found", "no entries detected"))
    assert len(payloads) == 1
    state = str(payloads[0]["state"])
    assert "no rows found" in state
    assert "no entries detected" in state
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    assert question["type"] == "noul"  # type: ignore[index]
    instructions = str(question["instructions"]).lower()  # type: ignore[index]
    assert "same substantive outcome" in instructions
    assert "no new progress" in instructions
