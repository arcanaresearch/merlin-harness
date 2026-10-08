from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .chat import ChatMessage


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageLineage(ConversationModel):
    sequence: int = Field(ge=1)
    message_id: str
    source_message_id: str


class CompactionRecord(ConversationModel):
    id: str
    session_id: str
    source_start: int = Field(ge=1)
    source_end: int = Field(ge=1)
    source_digest: str = Field(min_length=64, max_length=64)
    summary: str
    summary_digest: str = Field(min_length=64, max_length=64)
    algorithm: str
    created_at: datetime | None = None


class ContextProjection(ConversationModel):
    session_id: str
    identity: ChatMessage
    messages: tuple[ChatMessage, ...]
    lineage: tuple[MessageLineage, ...]
    transcript_digest: str = Field(min_length=64, max_length=64)
    head_sequence: int = Field(ge=0)
    compaction: CompactionRecord | None = None
