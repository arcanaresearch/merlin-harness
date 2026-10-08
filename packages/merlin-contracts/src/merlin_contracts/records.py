from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class SessionRecord(RecordModel):
    id: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageRecord(RecordModel):
    id: str
    session_id: str
    sequence: int = Field(ge=0)
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    created_at: datetime
    tool_call_id: str | None = None


class EventRecord(RecordModel):
    id: str
    session_id: str
    sequence: int = Field(ge=0)
    type: str
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
