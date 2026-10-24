from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
