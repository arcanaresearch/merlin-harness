import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FindingSeverity = Literal["informational", "low", "medium", "high", "critical"]
FindingStatus = Literal["draft", "accepted", "rejected"]
FindingValidation = Literal["unvalidated", "validated", "refuted", "inconclusive", "partial"]
ObservationOutcome = Literal["observed", "not_observed", "inconclusive", "error", "partial"]
AbsenceAuthority = Literal["none", "bounded", "exhaustive"]
ReportFormat = Literal["html", "markdown", "csv", "json"]
ReportAudience = Literal["internal", "customer"]


class FindingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class EvidenceWitness(FindingContract):
    """A content-addressed evidence reference that cannot silently change."""

    reference: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source: str = Field(min_length=1)


class AttackPathStep(FindingContract):
    ordinal: int = Field(ge=0)
    action: str = Field(min_length=1)
    observation: str = Field(min_length=1)
    witness_references: list[str] = Field(default_factory=list)


class CampaignObservation(FindingContract):
    id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    technique_id: str = Field(min_length=1)
    outcome: ObservationOutcome
    summary: str = Field(min_length=1)
    absence_authority: AbsenceAuthority = "none"
    coverage: str | None = None
    witnesses: list[EvidenceWitness] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_absence_claim(self) -> "CampaignObservation":
        if self.outcome == "not_observed" and self.absence_authority == "none":
            raise ValueError("not_observed requires bounded or exhaustive absence authority")
        if self.outcome != "not_observed" and self.absence_authority != "none":
            raise ValueError("absence authority is only valid for not_observed outcomes")
        if self.absence_authority != "none" and not (self.coverage or "").strip():
            raise ValueError("an absence claim requires its tested coverage")
        return self


class PartialComplianceSignal(FindingContract):
    """Graded partial engagement: what the target did versus what it refused.

    A partial signal is recorded progress, never a verdict: it must not be
    reported as a confirmed or refuted outcome.
    """

    observed: str = Field(min_length=1)
    refused: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    graded_by: str | None = None


class FindingDraft(FindingContract):
    title: str
    severity: FindingSeverity
    summary: str
    reproduction: str
    remediation: str
    creator: str
    evidence_references: list[str] = Field(default_factory=list)
    target_id: str = "unspecified"
    technique_id: str = "unspecified"
    validation: FindingValidation = "unvalidated"
    validation_summary: str | None = None
    witnesses: list[EvidenceWitness] = Field(default_factory=list)
    attack_path: list[AttackPathStep] = Field(default_factory=list)
    causal_ambiguity: bool = False
    impact_level: int = Field(default=0, ge=0, le=4)
    requires_independent_reproduction: bool = False
    partial_signal: PartialComplianceSignal | None = None

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> "FindingDraft":
        ordinals = [step.ordinal for step in self.attack_path]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("attack path ordinals must be contiguous from zero")
        if self.validation != "unvalidated" and not (self.validation_summary or "").strip():
            raise ValueError("validation and refutation require a summary")
        if self.validation == "partial" and self.partial_signal is None:
            raise ValueError("partial validation requires a partial compliance signal")
        if self.validation in {"validated", "refuted"} and self.partial_signal is not None:
            raise ValueError("a partial compliance signal is not a validation verdict")
        evidence = set(self.evidence_references)
        unowned = {
            reference
            for step in self.attack_path
            for reference in step.witness_references
            if reference not in evidence
        }
        if unowned:
            raise ValueError("attack path references evidence outside the finding")
        return self

    def stable_identity(self) -> str:
        """Identity the security claim, independent of author and mutable prose."""
        body = json.dumps(
            {
                "target": self.target_id.strip().casefold(),
                "technique": self.technique_id.strip().casefold(),
                "title": " ".join(self.title.split()).casefold(),
                "reproduction": " ".join(self.reproduction.split()).casefold(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


class Finding(FindingDraft):
    id: str
    status: FindingStatus = "draft"
    identity: str | None = None


class ReportRequest(FindingContract):
    session_id: str
    format: ReportFormat = "html"
    audience: ReportAudience = "internal"
    output: Path = Path("reports/merlin-report.html")


class ReportResult(FindingContract):
    format: ReportFormat
    output: Path
    finding_count: int = Field(ge=0)
