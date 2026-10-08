"""Refusal-judge backend selection precedence in the composition root."""

from __future__ import annotations

from types import SimpleNamespace

from merlin_agents.judgments import ChatRefusalJudge, JevRefusalJudge, LayaRefusalJudge
from merlin_config import JudgmentsConfig
from merlin_tui.bootstrap import _select_refusal_judge


def _config(judgments: JudgmentsConfig) -> SimpleNamespace:
    return SimpleNamespace(
        judgments=judgments,
        provider_role_models={"routine": "routine-model"},
    )


def test_jev_wins_when_token_configured() -> None:
    judge = _select_refusal_judge(
        _config(
            JudgmentsConfig(
                jev_token="jev-key",
                laya_url="http://laya:8080",
                laya_token="laya-key",
            )
        ),
        provider=None,
        model="worker-model",
    )
    assert isinstance(judge, JevRefusalJudge)


def test_laya_when_no_jev_token() -> None:
    judge = _select_refusal_judge(
        _config(JudgmentsConfig(laya_url="http://laya:8080", laya_token="laya-key")),
        provider=None,
        model="worker-model",
    )
    assert isinstance(judge, LayaRefusalJudge)


def test_chat_fallback_when_no_judgment_keys() -> None:
    judge = _select_refusal_judge(
        _config(JudgmentsConfig(laya_url="http://laya:8080")),
        provider=object(),
        model="worker-model",
    )
    assert isinstance(judge, ChatRefusalJudge)
