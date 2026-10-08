"""Unit tests for the Jev shadow-mode specialist routing calibration seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import merlin_agents.specialist_templates as specialist_templates
import pytest
from merlin_agents.jev import JevClient
from merlin_agents.specialist_templates import (
    route_specialty_tags,
    route_specialty_tags_shadow,
    template_by_id,
    template_technique_routing,
    template_technique_routing_shadow,
)
from merlin_agents.specialists import AssignmentTechnique

_TEMPLATE = template_by_id("merlin-core-specialists", 1)


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


@pytest.fixture
def judgments(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    monkeypatch.setattr(
        specialist_templates, "emit_judgment", lambda **kwargs: records.append(kwargs)
    )
    return records


@pytest.fixture(autouse=True)
def _reset_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(specialist_templates, "_jev_client", None)
    monkeypatch.setattr(specialist_templates, "_jev_client_checked", True)


def _technique(id: str, *tags: str) -> AssignmentTechnique:
    return AssignmentTechnique(id, id, frozenset(tags), (f"pack:{id}",))


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def test_shadow_returns_tag_pick_regardless_of_jev_pick(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    _stub_jev_post(monkeypatch, ("injection", 0.9))
    result = _run(
        route_specialty_tags_shadow(
            frozenset({"tenant", "authorization"}),
            _TEMPLATE,
            client=client,
            campaign_id="campaign-1",
            task_id="t1",
        )
    )
    assert result == "auth-session"
    assert len(judgments) == 1
    record = judgments[0]
    assert record["verdict"] == "tag_pick=auth-session|jev_pick=injection"
    assert record["graded_by"] == "jev-shadow"
    assert record["confidence"] == 0.9
    assert record["input_tokens"] == 3
    assert record["output_tokens"] == 1


def test_shadow_agreement_recorded(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    _stub_jev_post(monkeypatch, ("auth-session", 0.95))
    result = _run(
        route_specialty_tags_shadow(
            frozenset({"tenant", "authorization"}), _TEMPLATE, client=client
        )
    )
    assert result == "auth-session"
    assert judgments[0]["verdict"] == "tag_pick=auth-session|jev_pick=auth-session"


def test_transport_failure_keeps_tag_pick_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    _stub_jev_post(monkeypatch, None)
    result = _run(
        route_specialty_tags_shadow(
            frozenset({"tenant", "authorization"}), _TEMPLATE, client=client
        )
    )
    assert result == "auth-session"
    assert judgments == []


def test_no_client_never_calls_transport(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    payloads = _stub_jev_post(monkeypatch, ("injection", 0.9))
    result = _run(route_specialty_tags_shadow(frozenset({"tenant", "authorization"}), _TEMPLATE))
    assert result == route_specialty_tags(frozenset({"tenant", "authorization"}), _TEMPLATE)
    assert payloads == []
    assert judgments == []


def test_fallback_pick_still_records_jev_comparison(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    _stub_jev_post(monkeypatch, ("injection", 0.7))
    result = _run(route_specialty_tags_shadow(frozenset({"quantum"}), _TEMPLATE, client=client))
    assert result == "adversarial-explorer"
    assert judgments[0]["verdict"] == "tag_pick=adversarial-explorer|jev_pick=injection"


def test_question_wording_and_criteria(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    payloads = _stub_jev_post(monkeypatch, ("injection", 0.9))
    _run(
        route_specialty_tags_shadow(frozenset({"prompt", "instruction"}), _TEMPLATE, client=client)
    )
    assert len(payloads) == 1
    question = payloads[0]["questions"]["question"]  # type: ignore[index]
    assert question["type"] == "choice"  # type: ignore[index]
    assert question["instructions"] == "Which specialist profile should own this task?"  # type: ignore[index]
    assert set(question["criteria"]) == {  # type: ignore[arg-type]
        entry.source_profile_id for entry in _TEMPLATE.entries
    }


def test_template_routing_shadow_matches_deterministic_partition(
    monkeypatch: pytest.MonkeyPatch, judgments: list[dict[str, object]]
) -> None:
    client = JevClient("test-token")
    payloads = _stub_jev_post(monkeypatch, ("injection", 0.9))
    techniques = (
        _technique("t-auth", "tenant", "authorization"),
        _technique("t-inject", "prompt", "instruction"),
        _technique("t-unknown", "quantum"),
    )
    shadow = _run(template_technique_routing_shadow(_TEMPLATE, techniques, client=client))
    assert shadow == template_technique_routing(_TEMPLATE, techniques)
    assert len(payloads) == 3
    assert len(judgments) == 3
    assert {record["task_id"] for record in judgments} == {"t-auth", "t-inject", "t-unknown"}
