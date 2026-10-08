from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InfrastructureFailureCategory(StrEnum):
    ADAPTER_TIMEOUT = "adapter_timeout"
    MISSING_RESPONSE_POINTER = "missing_response_pointer"
    DELIVERY_FAILURE = "delivery_failure"
    UNHEALTHY_TARGET_SESSION = "unhealthy_target_session"


class HypothesisState(StrEnum):
    UNTESTED = "untested"
    IN_PROGRESS = "in_progress"
    SUPPORTED = "supported"
    FALSIFIED = "falsified"
    BLOCKED_BY_INFRASTRUCTURE = "blocked_by_infrastructure"
    SUPERSEDED = "superseded"


class ArmOutcomeKind(StrEnum):
    AUTHORITATIVE_SUCCESS = "authoritative_success"
    VALIDATED_FINDING = "validated_finding"
    PARTIAL_SIGNAL = "partial_signal"
    CLEAN_NEGATIVE = "clean_negative"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    AMBIGUOUS = "ambiguous"


class LanguageEvidenceStatus(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNAVAILABLE = "unavailable"
    INCONCLUSIVE = "inconclusive"


class CampaignCounters(BaseModel):
    """Independent campaign work counters; target and model work never alias."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_calls: int = Field(default=0, ge=0)
    model_steps: int = Field(default=0, ge=0)
    validation_calls: int = Field(default=0, ge=0)
    reproduction_calls: int = Field(default=0, ge=0)
    infrastructure_failures: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class HypothesisRecord(BaseModel):
    """Immutable hypothesis identity plus its latest append-only lifecycle state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    scenario_fingerprint: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    state: HypothesisState = HypothesisState.UNTESTED
    parent_hypothesis_id: str | None = None
    creating_evidence_refs: tuple[str, ...] = Field(min_length=1)
    lineage: tuple[str, ...] = Field(min_length=1)
    state_evidence_refs: tuple[str, ...] = ()
    state_reason: str | None = None
    infrastructure_failure_category: InfrastructureFailureCategory | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lineage_and_evidence(self) -> HypothesisRecord:
        if self.lineage[-1] != self.id:
            raise ValueError("hypothesis lineage must end with its own identity")
        expected_parent = self.lineage[-2] if len(self.lineage) > 1 else None
        if self.parent_hypothesis_id != expected_parent:
            raise ValueError("hypothesis parent must be the immediate lineage predecessor")
        if len(set(self.lineage)) != len(self.lineage):
            raise ValueError("hypothesis lineage cannot contain cycles")
        if len(set(self.creating_evidence_refs)) != len(self.creating_evidence_refs):
            raise ValueError("hypothesis creating evidence references must be unique")
        if len(set(self.state_evidence_refs)) != len(self.state_evidence_refs):
            raise ValueError("hypothesis state evidence references must be unique")
        blocked = self.state == HypothesisState.BLOCKED_BY_INFRASTRUCTURE
        if blocked != (self.infrastructure_failure_category is not None):
            raise ValueError(
                "infrastructure failure category is required only for infrastructure blocks"
            )
        return self


class ArmIdentity(BaseModel):
    """Complete schedulable ARM identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1)
    scenario_fingerprint: str = Field(min_length=1)
    surface_id: str = Field(min_length=1)
    technique_id: str = Field(min_length=1)
    controller_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    actual_language: str = Field(min_length=1)
    state_id: str = Field(min_length=1)


class CampaignCounterEvent(BaseModel):
    """Idempotent append-only counter delta attributable to campaign work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    counters: CampaignCounters
    arm_id: str | None = None
    hypothesis_id: str | None = None
    technique_attempt_id: str | None = None
    infrastructure_failure_category: InfrastructureFailureCategory | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_delta(self) -> CampaignCounterEvent:
        if self.counters.total == 0:
            raise ValueError("campaign counter event must increment at least one counter")
        has_failure = self.counters.infrastructure_failures > 0
        if has_failure != (self.infrastructure_failure_category is not None):
            raise ValueError(
                "infrastructure failure category must accompany infrastructure failures"
            )
        return self


class TechniqueLanguageEvidence(BaseModel):
    """Append-only language evidence for one concrete technique attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    technique_attempt_id: str = Field(min_length=1)
    requested_language: str = Field(min_length=1)
    actual_language: str = Field(min_length=1)
    language_evidence_status: LanguageEvidenceStatus
    language_evidence_source: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> TechniqueLanguageEvidence:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("language evidence references must be unique")
        return self


class ArmOutcome(BaseModel):
    """Immutable attributable result for one concrete ARM execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    arm_id: str = Field(min_length=1)
    technique_attempt_id: str = Field(min_length=1)
    hypothesis_id: str | None = None
    identity: ArmIdentity
    requested_language: str = Field(min_length=1)
    actual_language: str = Field(min_length=1)
    language_evidence_status: LanguageEvidenceStatus
    language_evidence_source: str = Field(min_length=1)
    outcome: ArmOutcomeKind
    counters: CampaignCounters
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    signal: float | None = None
    distance_to_success: float | None = Field(default=None, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    model_cost_usd: float = Field(default=0.0, ge=0)
    usage: dict[str, object] = Field(default_factory=dict)
    infrastructure_failure_category: InfrastructureFailureCategory | None = None
    reason: str = Field(min_length=1)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> ArmOutcome:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("arm outcome evidence references must be unique")
        if self.actual_language != self.identity.actual_language:
            raise ValueError("arm outcome actual language must match its ARM identity")
        if self.counters.total == 0:
            raise ValueError("arm outcome must account for at least one unit of work")
        infrastructure_failure = self.outcome == ArmOutcomeKind.INFRASTRUCTURE_FAILURE
        if infrastructure_failure != (self.infrastructure_failure_category is not None):
            raise ValueError("infrastructure failure outcomes require exactly one failure category")
        if infrastructure_failure != (self.counters.infrastructure_failures > 0):
            raise ValueError(
                "infrastructure failure outcomes require an infrastructure-failure count"
            )
        return self
