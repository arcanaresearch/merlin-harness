from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PackContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AttackCase(PackContract):
    id: str
    title: str
    instructions: str
    objectives: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    turn_limit: int = Field(default=1, ge=1)
    success_hints: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    combines: list[str] = Field(default_factory=list)
    execution_role: Literal["unpaired", "direct", "control"] = "unpaired"
    control_case_id: str | None = None
    oracle_id: str | None = None
    comparison_supported: bool = True
    comparison_block_reason: str | None = None
    strategy_id: Literal["direct", "crescendo", "goat", "hydra-select"] = "direct"

    def model_post_init(self, __context: object) -> None:
        if self.execution_role == "control" and self.control_case_id is not None:
            raise ValueError("control cases cannot reference another control")
        if self.execution_role == "direct" and self.control_case_id is None:
            raise ValueError("direct cases require an explicit paired control")
        if self.execution_role != "unpaired" and self.oracle_id is None:
            raise ValueError("paired cases require an explicit oracle identity")
        if self.control_case_id is not None and not self.control_case_id.strip():
            raise ValueError("control case identity must not be blank")
        if self.oracle_id is not None and not self.oracle_id.strip():
            raise ValueError("oracle identity must not be blank")
        if not self.comparison_supported and not self.comparison_block_reason:
            raise ValueError("unsupported comparisons require an explicit blocked reason")


class AttackPack(PackContract):
    id: str
    version: str
    title: str
    target_kind: str
    instructions: str
    cases: list[AttackCase]
    objectives: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    turn_limit: int = Field(default=1, ge=1)
    success_hints: list[str] = Field(default_factory=list)


class PolicyControl(PackContract):
    """A reviewed policy risk that requires target-specific evidence to evaluate."""

    control_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authority_type: Literal["organization_policy"]
    rule: str = Field(min_length=1)
    binding_required: list[str] = Field(min_length=1)
    attack_objectives: list[str] = Field(min_length=1)
    benign_controls: list[str] = Field(min_length=1)
    confirm_when: list[str] = Field(min_length=1)
    inconclusive_when: list[str] = Field(min_length=1)
    evidence_requirements: list[str] = Field(min_length=1)
    required_oracle_class: str = Field(min_length=1)
    test_data_class: Literal["synthetic_only", "synthetic_or_operator_reviewed_only"]
    absence_authority: Literal["bounded_only"]
    limitations: str = Field(min_length=1)
    reporting: str = Field(min_length=1)

    @field_validator(
        "control_id",
        "title",
        "rule",
        "required_oracle_class",
        "limitations",
        "reporting",
    )
    @classmethod
    def require_non_blank_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("policy-control values must not be blank")
        return value

    @field_validator(
        "binding_required",
        "attack_objectives",
        "benign_controls",
        "confirm_when",
        "inconclusive_when",
        "evidence_requirements",
    )
    @classmethod
    def require_non_blank_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("policy-control list items must not be blank")
        return value


class PolicyControlEnvelope(PackContract):
    """Authenticated knowledge-pack identity bound to one policy control."""

    pack_id: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)
    pack_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entry_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    control: PolicyControl


class TargetProfile(PackContract):
    """Non-secret connection settings for an AI target."""

    id: str
    title: str
    base_url: str
    model: str
    browser_session_policy: Literal["ephemeral", "persistent"] = "ephemeral"


PackRunStatus = Literal["pending", "running", "completed", "failed", "interrupted"]
PackTurnRole = Literal["attacker", "target"]


class PackTurn(PackContract):
    id: str
    run_id: str
    sequence: int = Field(ge=0)
    role: PackTurnRole
    content: str
    created_at: datetime


class PackOutcome(PackContract):
    successful: bool
    summary: str


class PackRun(PackContract):
    id: str
    session_id: str
    pack_id: str
    case_id: str
    target_profile_id: str
    status: PackRunStatus
    started_at: datetime
    completed_at: datetime | None = None
    outcome: PackOutcome | None = None
