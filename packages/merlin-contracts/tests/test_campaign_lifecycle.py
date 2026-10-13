import pytest
from merlin_contracts.campaign_lifecycle import (
    CampaignTerminalContract,
    CampaignTerminalReason,
    map_legacy_terminal_reason,
    read_campaign_terminal_reason,
    require_campaign_terminal_reason,
)
from pydantic import ValidationError


def test_campaign_terminal_contract_accepts_only_closed_reasons() -> None:
    record = CampaignTerminalContract(
        reason="objective_satisfied",
        evidence_refs=("evidence:terminal",),
    )
    assert record.reason is CampaignTerminalReason.OBJECTIVE_SATISFIED

    with pytest.raises(ValidationError):
        CampaignTerminalContract(
            reason="completed_with_limitations",
            evidence_refs=("evidence:terminal",),
        )


@pytest.mark.parametrize(
    ("legacy", "expected", "lossy"),
    [
        ("saturated", "frontier_exhausted", False),
        ("capped", "budget_exhausted", False),
        ("operator_stopped", "operator_stopped", False),
        ("failed", "infrastructure_blocked_after_retry_policy", True),
        ("interrupted", "infrastructure_blocked_after_retry_policy", True),
        ("completed_with_limitations", "frontier_exhausted", True),
        ("coverage_incomplete", "budget_exhausted", True),
    ],
)
def test_legacy_terminal_mapping_is_versioned_and_explicit(
    legacy: str, expected: str, lossy: bool
) -> None:
    mapping = map_legacy_terminal_reason(legacy)
    assert mapping.reason == expected
    assert mapping.lossy is lossy
    assert mapping.schema_version == "merlin.campaign-terminal-legacy-map.v1"


def test_legacy_terminal_mapping_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown legacy"):
        map_legacy_terminal_reason("mystery")


def test_current_terminal_reason_read_does_not_create_a_legacy_mapping() -> None:
    assert (
        read_campaign_terminal_reason("target_terminal") is CampaignTerminalReason.TARGET_TERMINAL
    )


def test_legacy_read_is_explicit_but_new_writes_reject_legacy_values() -> None:
    assert (
        read_campaign_terminal_reason("completed_with_limitations")
        is CampaignTerminalReason.FRONTIER_EXHAUSTED
    )
    with pytest.raises(ValueError, match="invalid current"):
        require_campaign_terminal_reason("completed_with_limitations")
