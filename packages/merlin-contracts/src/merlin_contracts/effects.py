from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ToolEffectState(StrEnum):
    INTENT = "intent"
    ACKNOWLEDGED = "acknowledged"
    AMBIGUOUS = "ambiguous"
    RECONCILED = "reconciled"


class ToolEffectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    session_id: str
    call_id: str
    agent_id: str | None = None
    attempt_id: str | None = None
    campaign_id: str | None = None
    target_id: str | None = None
    tool_name: str
    effect: str
    arguments_digest: str
    arguments_payload: dict[str, object] | None = None
    state: ToolEffectState
    result_content: str | None = None
    result_is_error: bool | None = None
    result_metadata: dict[str, object] = Field(default_factory=dict)
    reconciliation: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CleanupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    session_id: str
    resource_kind: str
    resource_id: str
    state: str
    details: dict[str, object] = Field(default_factory=dict)
    created_at: datetime | None = None
