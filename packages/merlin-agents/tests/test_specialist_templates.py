from dataclasses import FrozenInstanceError

import pytest
from merlin_agents.specialist_templates import (
    OPINIONATED_TEAM_TEMPLATES,
    STRUCTURAL_ROLE_IDS,
    SpecialistTeamTemplate,
    SpecialistTemplateEntry,
    compile_template_proposals,
    template_by_id,
)
from merlin_agents.specialists import (
    CORE_WORKER_PROFILES,
    is_dynamic_specialist_label,
)

_CATEGORY_IDS = (
    "auth-session",
    "injection",
    "data-exposure",
    "business-logic",
    "capability-abuse",
)
_PROFILES = {profile.id: profile for profile in CORE_WORKER_PROFILES}
_COMPILE_KWARGS = {
    "campaign_id": "campaign-1",
    "source_task_ids": ("recon-task-1",),
    "evidence_refs": ("evidence://recon/manifest",),
    "evidence_expectation": "attributable recon evidence for every claim",
}


def test_shipped_template_is_versioned_and_immutable() -> None:
    template = template_by_id("merlin-core-specialists", 1)
    assert template.version == 1
    assert len(template.entries) == 5
    with pytest.raises(FrozenInstanceError):
        template.version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        template.entries[0].objective = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        OPINIONATED_TEAM_TEMPLATES[("merlin-core-specialists", 2)] = template  # type: ignore[index]


def test_template_lookup() -> None:
    assert (
        template_by_id("merlin-core-specialists", 1)
        is OPINIONATED_TEAM_TEMPLATES[("merlin-core-specialists", 1)]
    )
    with pytest.raises(KeyError):
        template_by_id("merlin-core-specialists", 99)
    with pytest.raises(KeyError):
        template_by_id("unknown-template", 1)


def test_entries_match_source_category_profiles() -> None:
    template = template_by_id("merlin-core-specialists", 1)
    assert [entry.label for entry in template.entries] == [
        f"dynamic-{category}" for category in _CATEGORY_IDS
    ]
    for entry, category in zip(template.entries, _CATEGORY_IDS, strict=True):
        profile = _PROFILES[category]
        assert entry.objective == profile.instructions
        assert entry.specialty_tags == profile.specialty_tags


def test_entries_exclude_trust_boundary_roles() -> None:
    template = template_by_id("merlin-core-specialists", 1)
    for entry in template.entries:
        assert entry.label.removeprefix("dynamic-") not in STRUCTURAL_ROLE_IDS


def test_compilation_yields_valid_proposals() -> None:
    template = template_by_id("merlin-core-specialists", 1)
    proposals = compile_template_proposals(template, **_COMPILE_KWARGS)
    assert len(proposals) == len(template.entries)
    for proposal, entry in zip(proposals, template.entries, strict=True):
        assert is_dynamic_specialist_label(proposal.label)
        assert proposal.label == entry.label
        assert proposal.objective == entry.objective
        assert proposal.campaign_id == "campaign-1"
        assert proposal.source_task_ids == ("recon-task-1",)
        assert proposal.evidence_refs == ("evidence://recon/manifest",)
        assert proposal.ttl_waves is None
        assert entry.ttl_waves is None
        assert proposal.digest.startswith("sha256:")
    assert len({proposal.digest for proposal in proposals}) == len(proposals)


def test_compilation_rejects_structural_role_entries() -> None:
    bad = SpecialistTeamTemplate(
        id="bad-template",
        version=1,
        entries=(
            SpecialistTemplateEntry(
                label="dynamic-recon",
                objective="sneak structural recon into a template",
                specialty_tags=frozenset({"recon"}),
            ),
        ),
    )
    with pytest.raises(ValueError, match="trust-boundary"):
        compile_template_proposals(bad, **_COMPILE_KWARGS)


def test_entry_label_must_be_dynamic_slug() -> None:
    with pytest.raises(ValueError, match="dynamic- slug"):
        SpecialistTemplateEntry(
            label="auth-session",
            objective="missing the dynamic prefix",
            specialty_tags=frozenset(),
        )


def test_template_requires_unique_labels() -> None:
    entry = SpecialistTemplateEntry(
        label="dynamic-auth-session",
        objective="duplicate",
        specialty_tags=frozenset(),
    )
    with pytest.raises(ValueError, match="unique"):
        SpecialistTeamTemplate(id="dup", version=1, entries=(entry, entry))


def test_template_entries_carry_grant_shapes_not_tool_name_lists() -> None:
    entry = SpecialistTemplateEntry(
        label="dynamic-auth-session",
        objective="pin a grant shape",
        specialty_tags=frozenset({"session"}),
        grant_effects=frozenset({"target_read", "target_network"}),
    )
    assert not hasattr(entry, "tool_ids")
    with pytest.raises(ValueError, match="grant effects must not be blank"):
        SpecialistTemplateEntry(
            label="dynamic-auth-session",
            objective="blank effect",
            specialty_tags=frozenset(),
            grant_effects=frozenset({" "}),
        )

    template = SpecialistTeamTemplate(id="grant-shapes", version=1, entries=(entry,))
    (proposal,) = compile_template_proposals(template, **_COMPILE_KWARGS)
    assert proposal.grant_effects == frozenset({"target_read", "target_network"})

    # Entries without a pinned grant shape propose no effect set, so admission
    # inherits the parent grant.
    inherited = SpecialistTeamTemplate(
        id="grant-inherit",
        version=1,
        entries=(
            SpecialistTemplateEntry(
                label="dynamic-injection",
                objective="inherit the parent grant",
                specialty_tags=frozenset(),
            ),
        ),
    )
    (proposal,) = compile_template_proposals(inherited, **_COMPILE_KWARGS)
    assert proposal.grant_effects is None
