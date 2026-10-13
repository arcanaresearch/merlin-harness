"""Closed campaign terminal taxonomy with an explicit legacy translation."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CAMPAIGN_TERMINAL_SCHEMA_VERSION = "merlin.campaign-terminal.v1"
LEGACY_TERMINAL_MAPPING_VERSION = "merlin.campaign-terminal-legacy-map.v1"


class CampaignTerminalReason(StrEnum):
    OBJECTIVE_SATISFIED = "objective_satisfied"
    FRONTIER_EXHAUSTED = "frontier_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TARGET_TERMINAL = "target_terminal"
    OPERATOR_STOPPED = "operator_stopped"
    INFRASTRUCTURE_BLOCKED_AFTER_RETRY_POLICY = "infrastructure_blocked_after_retry_policy"


class CampaignTerminalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.campaign-terminal.v1"] = CAMPAIGN_TERMINAL_SCHEMA_VERSION
    reason: CampaignTerminalReason
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class LegacyTerminalMapping(BaseModel):
    """Auditable, potentially lossy translation from the former stop taxonomy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.campaign-terminal-legacy-map.v1"] = (
        LEGACY_TERMINAL_MAPPING_VERSION
    )
    legacy_reason: str = Field(min_length=1)
    reason: CampaignTerminalReason
    lossy: bool


_LEGACY_TERMINAL_REASONS: dict[str, tuple[CampaignTerminalReason, bool]] = {
    "saturated": (CampaignTerminalReason.FRONTIER_EXHAUSTED, False),
    "capped": (CampaignTerminalReason.BUDGET_EXHAUSTED, False),
    "operator_stopped": (CampaignTerminalReason.OPERATOR_STOPPED, False),
    "failed": (CampaignTerminalReason.INFRASTRUCTURE_BLOCKED_AFTER_RETRY_POLICY, True),
    "interrupted": (
        CampaignTerminalReason.INFRASTRUCTURE_BLOCKED_AFTER_RETRY_POLICY,
        True,
    ),
    "completed_with_limitations": (CampaignTerminalReason.FRONTIER_EXHAUSTED, True),
    "coverage_incomplete": (CampaignTerminalReason.BUDGET_EXHAUSTED, True),
}

CAMPAIGN_TERMINAL_REASONS = frozenset(reason.value for reason in CampaignTerminalReason)


def map_legacy_terminal_reason(value: str) -> LegacyTerminalMapping:
    """Translate known persisted legacy values; reject invented terminal states."""

    normalized = value.strip().casefold()
    try:
        reason, lossy = _LEGACY_TERMINAL_REASONS[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown legacy campaign stop reason: {value}") from exc
    return LegacyTerminalMapping(
        legacy_reason=normalized,
        reason=reason,
        lossy=lossy,
    )


def read_campaign_terminal_reason(value: str) -> CampaignTerminalReason:
    """Read a current reason or explicitly translate a persisted legacy value."""

    normalized = value.strip().casefold()
    try:
        return CampaignTerminalReason(normalized)
    except ValueError:
        return map_legacy_terminal_reason(normalized).reason


def require_campaign_terminal_reason(value: str) -> CampaignTerminalReason:
    """Validate a new lifecycle write against the closed current taxonomy."""

    normalized = value.strip().casefold()
    try:
        return CampaignTerminalReason(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid current campaign terminal reason: {value}") from exc
