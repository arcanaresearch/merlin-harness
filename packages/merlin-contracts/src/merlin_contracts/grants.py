from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CapabilityGrant(BaseModel):
    """Effect-group authority grant.

    ``effect_classes`` carries ``PermissionEffect`` values as strings so
    merlin-contracts stays free of a merlin-permissions dependency; the
    permissions layer converts them when resolving the grant.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    effect_classes: frozenset[str] = Field(min_length=1)
    target_scope: str | None = Field(default=None, min_length=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    evidence: str | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None
