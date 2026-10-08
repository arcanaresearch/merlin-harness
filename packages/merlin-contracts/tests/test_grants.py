from datetime import UTC, datetime

import pytest
from merlin_contracts import CapabilityGrant
from pydantic import ValidationError


def test_capability_grant_round_trip() -> None:
    grant = CapabilityGrant(
        id="grant-1",
        effect_classes=frozenset({"target_read", "provider_network"}),
        target_scope="target-9",
        max_concurrency=2,
        evidence="delivery_receipt",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    restored = CapabilityGrant.model_validate(grant.model_dump())
    assert restored == grant
    assert restored.effect_classes == frozenset({"target_read", "provider_network"})
    again = CapabilityGrant.model_validate_json(grant.model_dump_json())
    assert again == grant


def test_capability_grant_defaults_are_optional() -> None:
    grant = CapabilityGrant(id="grant-2", effect_classes=frozenset({"workspace_read"}))
    assert grant.target_scope is None
    assert grant.max_concurrency is None
    assert grant.evidence is None
    assert grant.expires_at is None


def test_capability_grant_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant.model_validate({"id": "g", "effect_classes": ["target_read"], "bogus": 1})


@pytest.mark.parametrize(
    "payload",
    (
        {"id": "", "effect_classes": ["target_read"]},
        {"id": "g", "effect_classes": []},
        {"id": "g", "effect_classes": ["target_read"], "target_scope": ""},
        {"id": "g", "effect_classes": ["target_read"], "max_concurrency": 0},
        {"id": "g", "effect_classes": ["target_read"], "evidence": ""},
    ),
)
def test_capability_grant_rejects_empty_or_invalid_authority(payload: object) -> None:
    with pytest.raises(ValidationError):
        CapabilityGrant.model_validate(payload)
