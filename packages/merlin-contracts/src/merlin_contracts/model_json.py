"""Tolerant parsing for JSON emitted by model providers.

Provider output dialects differ: DeepSeek returns bare JSON, Anthropic models
commonly wrap decisions in Markdown code fences, and some models add leading
prose. Structured-output consumers must never call ``json.loads`` on raw model
text — route every model reply through :func:`parse_model_json` instead.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["ModelJsonError", "parse_model_json", "strip_code_fences"]


class ModelJsonError(ValueError):
    """Raised when a model reply cannot be parsed as JSON.

    The message quotes the head of the actual reply so "empty response",
    "refusal", and "wrong dialect" are distinguishable in logs.
    """


def strip_code_fences(text: str) -> str:
    """Unwrap a Markdown code fence some providers put around a JSON reply."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    body = stripped[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _json_candidates(text: str) -> list[str]:
    """Candidate JSON strings from a model reply, most to least specific."""
    candidates = [strip_code_fences(text)]
    # A fenced block anywhere in the reply (models may prepend a Markdown
    # heading or commentary before the fence).
    fence = text.find("```")
    if fence != -1:
        candidates.append(strip_code_fences(text[fence:]))
    # Prose-padded bare JSON: first opening to last closing brace.
    stripped = candidates[0]
    start = min(
        (index for index in (stripped.find("{"), stripped.find("[")) if index != -1),
        default=-1,
    )
    if start != -1:
        end = max(stripped.rfind("}"), stripped.rfind("]"))
        if end > start:
            candidates.append(stripped[start : end + 1])
    return candidates


def parse_model_json(text: str) -> Any:
    """Parse a model reply as JSON, tolerating fences and prose padding.

    Raises :class:`ModelJsonError` quoting the reply head on failure.
    """
    first_error: Exception | None = None
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            if first_error is None:
                first_error = error
    head = text.strip()[:80].replace("\n", "\\n")
    raise ModelJsonError(
        f"model reply is not parseable JSON ({first_error}); reply head: {head!r}"
    ) from first_error
