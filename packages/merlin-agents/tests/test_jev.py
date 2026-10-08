"""Unit tests for the shared TypeSafe Jev decision client."""

from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest
from merlin_agents.jev import JevClient


def _run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _stub_urlopen(monkeypatch, payload, recorder: list):  # type: ignore[no-untyped-def]
    def _urlopen(request, timeout):  # noqa: ANN001, ANN202
        recorder.append(request)
        if isinstance(payload, Exception):
            raise payload
        return _FakeHttpResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)


def _payload(answer: dict, usage: dict | None = None) -> bytes:
    body = {
        "answers": {"question": answer},
        "usage": usage or {"input_tokens": 100, "output_tokens": 10},
    }
    return json.dumps(body).encode()


def test_from_config() -> None:
    class _Config:
        jev_url = "https://jev.example/v1/systemone"
        jev_token: str | None = "tok"

    client = JevClient.from_config(_Config())
    assert client is not None
    assert client._url == "https://jev.example/v1/systemone"

    _Config.jev_token = None
    assert JevClient.from_config(_Config()) is None


def test_noul_happy_path(monkeypatch) -> None:  # noqa: ANN001
    recorder: list = []
    _stub_urlopen(monkeypatch, _payload({"type": "noul", "noul": 0.87}), recorder)
    client = JevClient("secret-token")

    probability = _run(
        client.noul(
            "some state",
            "Did the assistant refuse?",
            true_meaning="It refused",
            false_meaning="It engaged",
        )
    )

    assert probability == pytest.approx(0.87)
    request = recorder[0]
    assert request.full_url == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["Authorization"] == "Bearer secret-token"
    body = json.loads(request.data.decode())
    assert body["model"] == "jev-latest"
    question = body["questions"]["question"]
    assert question["type"] == "noul"
    assert question["criteria"] == {"true": "It refused", "false": "It engaged"}
    assert client.input_tokens == 100
    assert client.output_tokens == 10


def test_choice_happy_path(monkeypatch) -> None:  # noqa: ANN001
    recorder: list = []
    answer = {
        "type": "choice",
        "choice": "escalate",
        "probabilities": {"escalate": 0.7, "continue": 0.3},
        "confidence": 0.62,
    }
    _stub_urlopen(monkeypatch, _payload(answer), recorder)
    client = JevClient("t")

    result = _run(client.choice("state", "What next?", {"escalate": "hand off", "continue": None}))

    assert result is not None
    option, confidence = result
    assert option == "escalate"
    assert confidence == pytest.approx(0.62)
    body = json.loads(recorder[0].data.decode())
    assert body["questions"]["question"]["type"] == "choice"
    assert body["questions"]["question"]["criteria"] == {
        "escalate": "hand off",
        "continue": None,
    }


def test_score_happy_path(monkeypatch) -> None:  # noqa: ANN001
    recorder: list = []
    answer = {
        "type": "score",
        "score": 2.0,
        "legend": ["low", "mid", "high"],
        "probabilities": {"low": 0.1, "mid": 0.2, "high": 0.7},
        "confidence": 0.55,
    }
    _stub_urlopen(monkeypatch, _payload(answer), recorder)
    client = JevClient("t")

    result = _run(client.score("state", "How severe?", ["low", "mid", "high"]))

    assert result is not None
    score, confidence = result
    assert score == pytest.approx(2.0)
    assert confidence == pytest.approx(0.55)
    body = json.loads(recorder[0].data.decode())
    assert body["questions"]["question"]["levels"] == ["low", "mid", "high"]


def test_failures_return_none(monkeypatch) -> None:  # noqa: ANN001
    recorder: list = []
    client = JevClient("t")

    _stub_urlopen(monkeypatch, OSError("connection refused"), recorder)
    assert _run(client.noul("s", "q?")) is None
    assert _run(client.choice("s", "q?", {"a": None})) is None
    assert _run(client.score("s", "q?", ["a", "b"])) is None

    # Malformed payloads also return None.
    _stub_urlopen(monkeypatch, b'{"answers": {}}', recorder)
    assert _run(client.noul("s", "q?")) is None

    _stub_urlopen(monkeypatch, _payload({"type": "choice", "choice": "unknown"}), recorder)
    assert _run(client.choice("s", "q?", {"a": None})) is None

    # Usage accumulates on every successful HTTP response, even when the
    # answer itself fails validation (the tokens were still consumed).
    _stub_urlopen(monkeypatch, _payload({"type": "noul", "noul": 0.5}), recorder)
    assert _run(client.noul("s", "q?")) == pytest.approx(0.5)
    assert client.input_tokens == 200


def test_retry_on_retryable_status(monkeypatch) -> None:  # noqa: ANN001
    calls: list = []

    def _urlopen(request, timeout):  # noqa: ANN001, ANN202
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 529, "Overloaded", None, None)
        return _FakeHttpResponse(_payload({"type": "noul", "noul": 0.9}))

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    client = JevClient("t")

    assert _run(client.noul("s", "q?")) == pytest.approx(0.9)
    assert len(calls) == 2
