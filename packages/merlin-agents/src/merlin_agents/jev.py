"""TypeSafe Jev typed-decision client.

Shared transport and typed helpers for the Jev decision API
(``POST {url}`` with ``{"state", "model", "questions"}``).  Jev answers are
advisory: every helper returns ``None`` on any transport or schema failure so
callers fall back to deterministic behavior.  Cumulative token usage is
tracked on the client for telemetry.

- ``noul`` — yes/no question, returns the probability of "yes" (0..1).
- ``choice`` — one option from a criteria map, returns ``(option, confidence)``.
- ``score`` — ordered levels, returns ``(score, confidence)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, Self, runtime_checkable

_logger = logging.getLogger(__name__)

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
_RETRYABLE_STATUS_CODES = {429, 529}


@runtime_checkable
class JevSettings(Protocol):
    """Subset of the judgments configuration the client needs."""

    jev_url: str
    jev_token: str | None


def jev_post(
    url: str,
    token: str,
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> Mapping[str, object]:
    """POST one Jev request with one retry on retryable status codes."""
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                value = json.loads(response.read().decode())
            if not isinstance(value, Mapping):
                raise ValueError("jev returned a non-object response")
            return value
        except urllib.error.HTTPError as error:
            if error.code not in _RETRYABLE_STATUS_CODES or attempt:
                raise
            time.sleep(1.0)
    raise AssertionError("unreachable")


class JevClient:
    """Shared Jev decision client with cumulative usage tracking."""

    def __init__(
        self,
        token: str,
        url: str = DEFAULT_URL,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self.input_tokens = 0
        self.output_tokens = 0

    @classmethod
    def from_config(cls, config: JevSettings) -> Self | None:
        """Build a client from judgments config; ``None`` when no token is set."""
        token = getattr(config, "jev_token", None)
        if not token:
            return None
        return cls(token=token, url=str(config.jev_url))

    async def _ask(self, state: str, questions: Mapping[str, object]) -> Mapping[str, Any] | None:
        payload: dict[str, object] = {
            "state": state,
            "model": DEFAULT_MODEL,
            "questions": questions,
        }
        try:
            result = await asyncio.to_thread(
                jev_post, self._url, self._token, payload, self._timeout
            )
        except Exception:  # noqa: BLE001 - decisions are advisory; never raise
            _logger.debug("jev decision call failed", exc_info=True)
            return None
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
        answers = result.get("answers")
        return answers if isinstance(answers, Mapping) else None

    async def noul(
        self,
        state: str,
        question: str,
        *,
        true_meaning: str | None = None,
        false_meaning: str | None = None,
    ) -> float | None:
        """Ask a yes/no question; return the probability of "yes" or ``None``."""
        criteria = {
            "true": true_meaning or "The statement is true",
            "false": false_meaning or "The statement is false",
        }
        answers = await self._ask(
            state,
            {"question": {"type": "noul", "instructions": question, "criteria": criteria}},
        )
        answer = answers.get("question") if answers else None
        if not isinstance(answer, Mapping):
            return None
        try:
            return float(answer.get("noul"))
        except (TypeError, ValueError):
            return None

    async def choice(
        self,
        state: str,
        question: str,
        options: Mapping[str, str | None],
    ) -> tuple[str, float] | None:
        """Ask a typed choice; return ``(option, confidence)`` or ``None``."""
        answers = await self._ask(
            state,
            {"question": {"type": "choice", "instructions": question, "criteria": dict(options)}},
        )
        answer = answers.get("question") if answers else None
        if not isinstance(answer, Mapping):
            return None
        option = str(answer.get("choice", "")).strip()
        if not option or option not in options:
            return None
        try:
            confidence = float(answer.get("confidence"))
        except (TypeError, ValueError):
            return None
        return option, confidence

    async def score(
        self,
        state: str,
        question: str,
        levels: Sequence[str],
    ) -> tuple[float, float] | None:
        """Ask an ordered-level score; return ``(score, confidence)`` or ``None``."""
        answers = await self._ask(
            state,
            {
                "question": {
                    "type": "score",
                    "instructions": question,
                    "levels": list(levels),
                }
            },
        )
        answer = answers.get("question") if answers else None
        if not isinstance(answer, Mapping):
            return None
        try:
            return float(answer.get("score")), float(answer.get("confidence"))
        except (TypeError, ValueError):
            return None
