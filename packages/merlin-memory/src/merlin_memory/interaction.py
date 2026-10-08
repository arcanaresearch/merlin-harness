"""Verified, target-scoped browser interaction memory.

These records deliberately contain executable interaction structure rather than
worker-authored prose.  A worker result is not reusable memory until a validator
or operator has confirmed that the sequence reached the stated outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PLACEHOLDER = "{{message}}"
_SECRET_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "auth", "authorization", "password", "secret", "token"}
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{8,}|(?:sk|pk)-[a-z0-9_-]{8,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)


class InteractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InteractionStep(InteractionModel):
    """One replayable UI action with no captured user or credential value."""

    action: Literal[
        "navigate",
        "snapshot",
        "click",
        "fill",
        "press",
        "wait",
        "assert_visible",
        "assert_text",
    ]
    locator: str | None = Field(default=None, max_length=2_048)
    value: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="after")
    def keep_steps_replayable_and_non_secret(self) -> InteractionStep:
        for field_name, value in (("locator", self.locator), ("value", self.value)):
            if value is not None and _SECRET_TEXT.search(value):
                raise ValueError(f"interaction {field_name} contains secret-like material")
        if self.action == "fill" and self.value != _PLACEHOLDER:
            raise ValueError("reusable fill steps must use the {{message}} placeholder")
        if self.action == "navigate":
            if not self.value:
                raise ValueError("navigate steps require a URL")
            parsed = urlsplit(self.value)
            if parsed.username or parsed.password:
                raise ValueError("navigation URLs cannot contain credentials")
            if any(
                key.lower().replace("-", "_") in _SECRET_KEYS for key, _ in parse_qsl(parsed.query)
            ):
                raise ValueError("navigation URLs cannot contain secret-like query parameters")
        if self.action in {"click", "fill", "assert_visible", "assert_text"} and not self.locator:
            raise ValueError(f"{self.action} steps require a locator")
        if self.action == "press" and not self.value:
            raise ValueError("press steps require a key")
        return self


class VerifiedInteractionLesson(InteractionModel):
    """A successful interaction recipe admitted by a non-worker verifier."""

    id: str = Field(min_length=1, max_length=256)
    target_id: str = Field(min_length=1, max_length=512)
    interface_key: str = Field(min_length=1, max_length=256)
    steps: tuple[InteractionStep, ...] = Field(min_length=2, max_length=64)
    outcome: str = Field(min_length=1, max_length=1_024)
    evidence_references: tuple[str, ...] = Field(min_length=1, max_length=64)
    verified_by: str = Field(min_length=1, max_length=256)
    verified_at: datetime
    source_campaign_id: str = Field(min_length=1, max_length=256)
    source_task_id: str = Field(min_length=1, max_length=256)
    redactions: tuple[str, ...] = Field(default=(), max_length=64)
    digest: str = ""

    @model_validator(mode="after")
    def verify_attestation_and_digest(self) -> VerifiedInteractionLesson:
        if not self.verified_by.startswith(("operator:", "validator:")):
            raise ValueError("interaction lessons require operator or validator verification")
        if self.verified_at.tzinfo is None:
            raise ValueError("verified_at must be timezone-aware")
        if _SECRET_TEXT.search(self.outcome):
            raise ValueError("interaction outcome contains secret-like material")
        if not any(step.action == "fill" for step in self.steps):
            raise ValueError("interaction lessons must describe message input")
        if not any(step.action in {"press", "click"} for step in self.steps):
            raise ValueError("interaction lessons must describe submission")
        if not any(step.action in {"assert_visible", "assert_text"} for step in self.steps):
            raise ValueError("interaction lessons must verify the sent outcome")
        expected = interaction_lesson_digest(self)
        if self.digest and self.digest != expected:
            raise ValueError("interaction lesson digest mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        return self


def interaction_lesson_digest(lesson: VerifiedInteractionLesson) -> str:
    document = lesson.model_dump(mode="json", exclude={"digest"})
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
