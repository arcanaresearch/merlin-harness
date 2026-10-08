"""Versioned opinionated team templates for dynamic specialist composition.

Opinionated composition compiles a reviewed team template into wave-0
``DynamicSpecialistProposal`` objects that flow through the same
``compile_dynamic_specialist`` admission path as autonomous proposals.
Template entries are converted from the category profiles in
``CORE_WORKER_PROFILES``; trust-boundary roles (recon, validator,
remediation, automation, adversarial exploration) stay structural and are
rejected from template data.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

from .jev import JevClient
from .specialists import (
    CORE_WORKER_PROFILES,
    AssignmentTechnique,
    DynamicSpecialistProposal,
    is_dynamic_specialist_label,
)
from .telemetry import emit_judgment

# Roles that carry trust boundaries and must stay structural, never template
# data: reconnaissance feeds every specialist, validation and remediation
# gate findings, automation artifacts are independently audited, and the
# adversarial exploration profiles pursue open-ended hypotheses.
STRUCTURAL_ROLE_IDS = frozenset(
    {
        "recon",
        "independent-validator",
        "remediation-specialist",
        "automation-author",
        "automation-auditor",
        "adversarial-explorer",
        "adversarial-researcher",
        "adversarial-critic",
    }
)

_CATEGORY_PROFILES = {profile.id: profile for profile in CORE_WORKER_PROFILES}


@dataclass(frozen=True, slots=True)
class SpecialistTemplateEntry:
    """One reviewed specialist slot inside a team template.

    ``label`` must satisfy the dynamic-specialist slug rules; ``objective``
    is the cognitive brief reused as the proposal objective. The narrowing
    tuples optionally pin the proposal's technique/guidance/skill/authority
    sets; ``grant_effects`` optionally pins the grant shape (effect classes)
    the proposal requests, never a tool-name list. ``source_profile_id``
    records the category profile the entry was converted from, so the
    compiler can resolve the fixed path's per-profile model route for the
    proposal.
    """

    label: str
    objective: str
    specialty_tags: frozenset[str]
    # Compatibility-only field for old templates. Shipped templates do not
    # impose a wave-age lifetime.
    ttl_waves: int | None = None
    technique_ids: tuple[str, ...] = ()
    guidance_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    authority_ids: tuple[str, ...] = ()
    grant_effects: frozenset[str] = frozenset()
    source_profile_id: str | None = None

    def __post_init__(self) -> None:
        if not is_dynamic_specialist_label(self.label):
            raise ValueError("template entry label must be a bounded dynamic- slug")
        if not self.objective.strip():
            raise ValueError("template entry objective must not be blank")
        if self.ttl_waves is not None and (
            isinstance(self.ttl_waves, bool)
            or not isinstance(self.ttl_waves, int)
            or self.ttl_waves < 1
        ):
            raise ValueError("legacy template entry TTL must be positive")
        object.__setattr__(self, "specialty_tags", frozenset(self.specialty_tags))
        object.__setattr__(self, "grant_effects", frozenset(self.grant_effects))
        if any(not str(effect).strip() for effect in self.grant_effects):
            raise ValueError("template entry grant effects must not be blank")
        for name in (
            "technique_ids",
            "guidance_ids",
            "skill_ids",
            "authority_ids",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class SpecialistTeamTemplate:
    """A versioned, immutable team template for opinionated composition."""

    id: str
    version: int
    entries: tuple[SpecialistTemplateEntry, ...]

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("team template requires an id")
        if self.version < 1:
            raise ValueError("team template version must be positive")
        object.__setattr__(self, "entries", tuple(self.entries))
        if not self.entries:
            raise ValueError("team template requires at least one entry")
        labels = [entry.label for entry in self.entries]
        if len(set(labels)) != len(labels):
            raise ValueError("team template entry labels must be unique")


def _category_entry(profile_id: str) -> SpecialistTemplateEntry:
    """Convert a CORE_WORKER_PROFILES category profile into a template entry.

    The entry objective reuses the profile's instructions text verbatim so
    opinionated wave-0 specialists behave like the fixed-path workers.
    """

    profile = _CATEGORY_PROFILES[profile_id]
    return SpecialistTemplateEntry(
        label=f"dynamic-{profile.id}",
        objective=profile.instructions,
        specialty_tags=profile.specialty_tags,
        source_profile_id=profile.id,
    )


_SHIPPED_TEMPLATE = SpecialistTeamTemplate(
    id="merlin-core-specialists",
    version=1,
    entries=(
        _category_entry("auth-session"),
        _category_entry("injection"),
        _category_entry("data-exposure"),
        _category_entry("business-logic"),
        _category_entry("capability-abuse"),
    ),
)

OPINIONATED_TEAM_TEMPLATES = MappingProxyType(
    {(template.id, template.version): template for template in (_SHIPPED_TEMPLATE,)}
)


def template_by_id(id: str, version: int) -> SpecialistTeamTemplate:
    """Look up a shipped team template by id and exact version."""

    template = OPINIONATED_TEAM_TEMPLATES.get((id, version))
    if template is None:
        raise KeyError(f"unknown opinionated team template {id!r} version {version}")
    return template


def _normalized_tokens(tags: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token for tag in tags for token in tag.lower().replace("-", " ").split() if token
    )


def route_specialty_tags(tags: frozenset[str], template: SpecialistTeamTemplate) -> str:
    """Route technique tags to the best-overlapping template entry's source profile.

    Deterministic exact tag-token overlap against the template's specialty
    areas, ordered by template entry order as the tie-break. Techniques with
    no positive overlap return ``"adversarial-explorer"``: the structural
    explorer lane owns whatever the composition did not claim. Trust-boundary
    roles are never returned because they are not template data.
    """

    normalized = _normalized_tokens(tags)
    score, _, selected = max(
        (
            (len(normalized & entry.specialty_tags), -index, entry.source_profile_id)
            for index, entry in enumerate(template.entries)
        ),
        key=lambda item: (item[0], item[1]),
    )
    if score == 0 or selected is None:
        return "adversarial-explorer"
    return selected


def template_technique_routing(
    template: SpecialistTeamTemplate,
    techniques: tuple[AssignmentTechnique, ...],
) -> dict[str, tuple[str, ...]]:
    """Partition technique ids across template entries by specialty overlap.

    Every technique routes to exactly one entry's source profile or to the
    ``"adversarial-explorer"`` fallback, so the union covers the admitted set
    exactly once. Keys are source profile ids (and the fallback); values are
    technique ids sorted for determinism. Entries that claim no technique are
    omitted.
    """

    routed: dict[str, list[str]] = {}
    for technique in sorted(techniques, key=lambda item: item.id):
        profile_id = route_specialty_tags(technique.tags, template)
        routed.setdefault(profile_id, []).append(technique.id)
    return {profile_id: tuple(ids) for profile_id, ids in routed.items()}


_JEV_ROUTING_QUESTION = "Which specialist profile should own this task?"

_jev_client: JevClient | None = None
_jev_client_checked = False


def _jev_routing_client() -> JevClient | None:
    """Lazily build the shared Jev client; ``None`` when unavailable/disabled."""
    global _jev_client, _jev_client_checked
    if _jev_client_checked:
        return _jev_client
    _jev_client_checked = True
    try:
        from merlin_config import load_config
    except ImportError:
        return None
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - advisory gate; never raise
        return None
    judgments = getattr(config, "judgments", None)
    if judgments is None or not getattr(judgments, "jev_decisions_enabled", False):
        return None
    _jev_client = JevClient.from_config(judgments)
    return _jev_client


def _routing_options(template: SpecialistTeamTemplate) -> dict[str, str]:
    """Jev choice criteria: source profile id -> specialty-tag description."""
    return {
        entry.source_profile_id: ", ".join(sorted(entry.specialty_tags))
        for entry in template.entries
        if entry.source_profile_id is not None
    }


async def route_specialty_tags_shadow(
    tags: frozenset[str],
    template: SpecialistTeamTemplate,
    *,
    client: JevClient | None = None,
    campaign_id: str = "",
    task_id: str = "",
) -> str:
    """Shadow-mode routing: the deterministic tag pick always owns the decision.

    Behaves exactly like :func:`route_specialty_tags`.  When a Jev client is
    available (``judgments.jev_decisions_enabled`` plus a configured token),
    one advisory ``choice`` question asks which specialist profile should own
    the task and the Jev pick is recorded against the tag pick via
    ``emit_judgment`` (``graded_by="jev-shadow"``) for calibration.  Any
    failure or ``None`` answer leaves behavior byte-identical, and no more
    than one Jev call is made per routing decision.
    """

    selected = route_specialty_tags(tags, template)
    if client is None:
        client = _jev_routing_client()
    options = _routing_options(template)
    if client is None or not options:
        return selected
    state = "task specialty tags: " + (", ".join(sorted(tags)) or "(none)")
    input_before, output_before = client.input_tokens, client.output_tokens
    started = time.monotonic()
    answer = await client.choice(state, _JEV_ROUTING_QUESTION, options)
    latency_ms = int((time.monotonic() - started) * 1000)
    if answer is None:
        return selected
    jev_pick, confidence = answer
    emit_judgment(
        campaign_id=campaign_id,
        task_id=task_id,
        technique_ids=tuple(sorted(tags)),
        verdict=f"tag_pick={selected}|jev_pick={jev_pick}",
        confidence=confidence,
        graded_by="jev-shadow",
        latency_ms=latency_ms,
        input_tokens=client.input_tokens - input_before,
        output_tokens=client.output_tokens - output_before,
    )
    return selected


async def template_technique_routing_shadow(
    template: SpecialistTeamTemplate,
    techniques: tuple[AssignmentTechnique, ...],
    *,
    client: JevClient | None = None,
    campaign_id: str = "",
) -> dict[str, tuple[str, ...]]:
    """Shadow-mode variant of :func:`template_technique_routing`.

    The partition is computed by the deterministic tag-overlap router
    byte-identically; each technique's routing decision additionally records
    one advisory Jev pick for calibration when a client is available.
    """

    routed: dict[str, list[str]] = {}
    for technique in sorted(techniques, key=lambda item: item.id):
        profile_id = await route_specialty_tags_shadow(
            technique.tags,
            template,
            client=client,
            campaign_id=campaign_id,
            task_id=technique.id,
        )
        routed.setdefault(profile_id, []).append(technique.id)
    return {profile_id: tuple(ids) for profile_id, ids in routed.items()}


def compile_template_proposals(
    template: SpecialistTeamTemplate,
    *,
    campaign_id: str,
    source_task_ids: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    evidence_expectation: str,
    model_for_profile: Callable[[str], str | None] | None = None,
) -> tuple[DynamicSpecialistProposal, ...]:
    """Compile a team template into one wave-0 proposal per entry.

    Wave-0 proposals may cite reviewed recon/manifest evidence rather than
    completed source tasks, so the caller supplies the shared source task
    ids, evidence references, and evidence expectation; admission through
    ``compile_dynamic_specialist`` validates every field as usual.

    ``model_for_profile`` resolves the fixed path's per-profile model route
    (``AssignmentContext.model_for``) for each entry's source profile, so the
    proposal requests the same campaign-admitted route the fixed assignment
    would have used. Entries without a resolver or without a route keep
    ``requested_model=None`` and inherit the parent model, matching the fixed
    path's default-route fallback.
    """

    proposals = []
    for entry in template.entries:
        if any(entry.label == f"dynamic-{role}" for role in STRUCTURAL_ROLE_IDS):
            raise ValueError(f"template entry {entry.label!r} is a structural trust-boundary role")
        requested_model = (
            None
            if model_for_profile is None or entry.source_profile_id is None
            else model_for_profile(entry.source_profile_id)
        )
        proposals.append(
            DynamicSpecialistProposal(
                campaign_id=campaign_id,
                label=entry.label,
                objective=entry.objective,
                source_task_ids=tuple(source_task_ids),
                evidence_refs=tuple(evidence_refs),
                evidence_expectation=evidence_expectation,
                ttl_waves=entry.ttl_waves,
                technique_ids=entry.technique_ids if entry.technique_ids else None,
                guidance_ids=entry.guidance_ids,
                skill_ids=entry.skill_ids,
                authority_ids=entry.authority_ids,
                grant_effects=entry.grant_effects or None,
                requested_model=requested_model,
            )
        )
    return tuple(proposals)
