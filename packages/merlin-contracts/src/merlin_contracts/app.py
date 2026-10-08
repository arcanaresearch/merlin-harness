from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AppCommand(BaseModel):
    """A parsed operator command crossing the TUI/kernel boundary."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: list[str] = Field(default_factory=list)


class AppEvent(BaseModel):
    """An ordered kernel event suitable for persistence and display."""

    model_config = ConfigDict(extra="forbid")

    type: str
    payload: dict[str, object] = Field(default_factory=dict)
    session_id: str | None = None
    occurred_at: datetime
