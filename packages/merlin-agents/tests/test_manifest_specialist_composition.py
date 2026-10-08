"""Tests for specialist-composition fields on CampaignManifest and exports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

import pytest
from merlin_agents import (
    AssignmentBudget,
    CampaignManifest,
    CampaignProfile,
    DynamicSpecialistPolicy,
    ReviewedTargetAdapter,
)


def _manifest(**overrides: object) -> CampaignManifest:
    kwargs = {
        "id": "campaign-composition",
        "session_id": "session-composition",
        "objective": "Assess the reviewed API",
        "profile": CampaignProfile.RECOMMENDED,
        "target": ReviewedTargetAdapter(
            "target-1", "http_api", "https://target.test/api", "target-model"
        ),
        "provider_id": "provider-1",
        "provider_model": "model-1",
        "provider_effort": "default",
        "authority_ids": ("session:session-composition", "target:target-1"),
        "catalogue": (),
        "skill_documents": (),
        "tool_ids": ("target_request",),
        "budget": AssignmentBudget(8, 8_000, 120),
    }
    kwargs.update(overrides)
    return CampaignManifest.create(**kwargs)  # type: ignore[arg-type]


def test_specialist_composition_defaults_to_opinionated() -> None:
    manifest = _manifest()
    assert manifest.specialist_composition == "opinionated"
    assert manifest.dynamic_specialist_policy is None
    manifest.validate()


def test_specialist_composition_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="specialist_composition"):
        _manifest(specialist_composition="freeform")


def test_composition_fields_are_sealed_into_digest() -> None:
    opinionated = _manifest()
    autonomous = _manifest(specialist_composition="autonomous")
    assert opinionated.digest != autonomous.digest
    autonomous.validate()

    policy = DynamicSpecialistPolicy(max_concurrent=2, max_per_campaign=4)
    with_policy = _manifest(specialist_composition="autonomous", dynamic_specialist_policy=policy)
    assert with_policy.digest != autonomous.digest
    with_policy.validate()

    other_policy = DynamicSpecialistPolicy(max_concurrent=3, max_per_campaign=4)
    assert (
        _manifest(
            specialist_composition="autonomous",
            dynamic_specialist_policy=other_policy,
        ).digest
        != with_policy.digest
    )


def test_validate_rejects_tampered_composition_digest() -> None:
    manifest = _manifest(specialist_composition="autonomous")
    tampered = replace(manifest, specialist_composition="opinionated")
    with pytest.raises(ValueError, match="digest mismatch"):
        tampered.validate()


def test_manifest_sealed_before_composition_binding_remains_valid() -> None:
    manifest = _manifest()
    legacy_values = {
        "id": manifest.id,
        "session_id": manifest.session_id,
        "objective": manifest.objective,
        "profile": manifest.profile.value,
        "target": asdict(manifest.target),
        "provider_id": manifest.provider_id,
        "provider_model": manifest.provider_model,
        "provider_effort": manifest.provider_effort,
        "authority_ids": manifest.authority_ids,
        "catalogue": [],
        "skill_documents": [],
        "tool_ids": manifest.tool_ids,
        "budget": asdict(manifest.budget),
    }
    legacy_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                legacy_values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
    assert legacy_digest != manifest.digest
    legacy = replace(manifest, digest=legacy_digest)
    legacy.validate()


def test_legacy_digest_fallback_does_not_cover_non_default_composition() -> None:
    manifest = _manifest(specialist_composition="autonomous")
    legacy = replace(manifest, digest=_manifest().digest)
    with pytest.raises(ValueError, match="digest mismatch"):
        legacy.validate()


def test_dynamic_specialist_policy_is_exported() -> None:
    import merlin_agents

    assert merlin_agents.DynamicSpecialistPolicy is DynamicSpecialistPolicy
    assert "DynamicSpecialistPolicy" in merlin_agents.__all__
