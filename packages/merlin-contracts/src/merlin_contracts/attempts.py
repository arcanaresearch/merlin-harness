from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TechniqueAttemptState(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    EFFECT_ACKNOWLEDGED = "effect_acknowledged"
    OBSERVED = "observed"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    AMBIGUOUS = "ambiguous"


class TechniqueAttemptControlKind(StrEnum):
    ATTACK = "attack"
    NEGATIVE_CONTROL = "negative_control"
    POSITIVE_CONTROL = "positive_control"
    VALIDATION = "validation"


TERMINAL_TECHNIQUE_ATTEMPT_STATES = frozenset(
    {
        TechniqueAttemptState.CONFIRMED,
        TechniqueAttemptState.REFUTED,
        TechniqueAttemptState.INCONCLUSIVE,
        # An attempt that ended with graded partial engagement is finished;
        # cell-level aggregation decides whether the cell stays retryable.
        TechniqueAttemptState.PARTIAL,
        TechniqueAttemptState.BLOCKED,
        TechniqueAttemptState.CANCELLED,
        TechniqueAttemptState.AMBIGUOUS,
    }
)


class TechniqueAttempt(BaseModel):
    """Authoritative identity and lifecycle join for one admitted target experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    wave: int = Field(ge=0)
    task_id: str = Field(min_length=1)
    technique_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    hypothesis_id: str | None = None
    parent_attempt_id: str | None = None
    transform_id: str | None = None
    language: str = Field(default="und", min_length=1)
    control_kind: TechniqueAttemptControlKind
    oracle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    scheduler_attempt_id: str | None = None
    state: TechniqueAttemptState = TechniqueAttemptState.PLANNED
    strategy_state: dict[str, object] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    usage: dict[str, object] = Field(default_factory=dict)
    terminal_reason: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> TechniqueAttempt:
        if self.parent_attempt_id == self.id:
            raise ValueError("a technique attempt cannot parent itself")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("technique attempt evidence references must be unique")
        terminal = self.state in TERMINAL_TECHNIQUE_ATTEMPT_STATES
        if terminal != (self.terminal_at is not None):
            raise ValueError("terminal technique-attempt state and terminal_at must agree")
        if terminal != bool((self.terminal_reason or "").strip()):
            raise ValueError("terminal technique-attempt state requires one terminal reason")
        if self.state != TechniqueAttemptState.PLANNED and self.activated_at is None:
            raise ValueError("a started technique attempt requires activated_at")
        return self
