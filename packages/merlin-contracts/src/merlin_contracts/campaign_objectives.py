"""Target-neutral durable objectives and deterministic campaign outcome projection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAMPAIGN_OBJECTIVE_SCHEMA_VERSION = "merlin.campaign-objective.v1"
CAMPAIGN_OUTCOME_SCHEMA_VERSION = "merlin.campaign-outcome.v1"
CAMPAIGN_OBJECTIVE_CONTRACT_SCHEMA_VERSION = "merlin.campaign-objective-contract.v1"


class ObjectiveKind(StrEnum):
    """What an objective measures, without naming a target or transport."""

    STATE = "state"
    TRANSITION = "transition"
    COVERAGE = "coverage"
    BEHAVIOR = "behavior"
    INVARIANT = "invariant"


class ObjectiveTemporalSemantics(StrEnum):
    """When evidence must hold for an objective to be satisfied."""

    POINT_IN_TIME = "point_in_time"
    TRANSITION = "transition"
    CUMULATIVE = "cumulative"
    THROUGHOUT_CAMPAIGN = "throughout_campaign"
    AT_CAMPAIGN_END = "at_campaign_end"


class ObjectiveProvenanceKind(StrEnum):
    OPERATOR = "operator"
    REVIEWED_RECONNAISSANCE = "reviewed_reconnaissance"
    IMPORTED_CONTRACT = "imported_contract"
    MODEL_PROPOSAL = "model_proposal"


class ObjectiveSelectorQuantifier(StrEnum):
    ONE = "one"
    ALL_LISTED = "all_listed"
    ALL_AVAILABLE = "all_available"
    INCLUSIVE_RANGE = "inclusive_range"


class ObjectiveSelectorResolution(StrEnum):
    SEALED = "sealed"
    ADAPTER_REQUIRED = "adapter_required"


class ObjectiveSelectorBindingStatus(StrEnum):
    UNRESOLVED = "unresolved"
    BOUND = "bound"


class ObjectiveSelectorBindingSourceKind(StrEnum):
    REVIEWED_ADAPTER = "reviewed_adapter"
    AUTHORITATIVE_DISCOVERY = "authoritative_discovery"
    REVIEWED_CATALOGUE = "reviewed_catalogue"


class ObjectiveSelectorBindingSource(BaseModel):
    """Authority that established a finite selector denominator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ObjectiveSelectorBindingSourceKind
    source_ref: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class ObjectiveSelectorBinding(BaseModel):
    """Finite candidate denominator for one selector or one scoped selector value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector_value: str = Field(min_length=1)
    candidate_ids: tuple[str, ...] = ()
    status: ObjectiveSelectorBindingStatus
    reason: str | None = None
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> ObjectiveSelectorBinding:
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("selector binding candidate ids must be unique")
        if self.status is ObjectiveSelectorBindingStatus.BOUND:
            if not self.evidence_refs:
                raise ValueError("bound selector values require authoritative evidence")
            if self.reason is not None:
                raise ValueError("bound selector values cannot carry an unresolved reason")
        else:
            if self.candidate_ids:
                raise ValueError("unresolved selector values cannot carry candidate ids")
            if not (self.reason and self.reason.strip()):
                raise ValueError("unresolved selector values require a reason")
        return self


class ObjectiveSourceSpan(BaseModel):
    """Exact operator-text span from which a requirement was compiled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> ObjectiveSourceSpan:
        if self.end <= self.start:
            raise ValueError("objective source span end must follow start")
        if self.end - self.start != len(self.text):
            raise ValueError("objective source span offsets must match its exact text")
        return self


class ObjectiveSelector(BaseModel):
    """Target-neutral selector whose concrete oracle may be supplied by an adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str = Field(min_length=1)
    quantifier: ObjectiveSelectorQuantifier
    resolution: ObjectiveSelectorResolution
    values: tuple[str, ...] = ()
    scope_dimension: str | None = None
    scope_values: tuple[str, ...] = ()
    bindings: tuple[ObjectiveSelectorBinding, ...] = ()
    binding_source: ObjectiveSelectorBindingSource | None = None
    source_span: ObjectiveSourceSpan

    @model_validator(mode="after")
    def validate_selector(self) -> ObjectiveSelector:
        if self.quantifier in {
            ObjectiveSelectorQuantifier.ALL_LISTED,
            ObjectiveSelectorQuantifier.INCLUSIVE_RANGE,
        } and not self.values:
            raise ValueError(f"{self.quantifier.value} selectors require sealed values")
        if self.quantifier is ObjectiveSelectorQuantifier.ALL_AVAILABLE:
            if self.values:
                raise ValueError("all_available selectors cannot invent sealed values")
            if self.resolution is not ObjectiveSelectorResolution.ADAPTER_REQUIRED:
                raise ValueError("all_available selectors require adapter resolution")
        if len(set(self.values)) != len(self.values):
            raise ValueError("objective selector values must be unique")
        binding_values = tuple(binding.selector_value for binding in self.bindings)
        if len(set(binding_values)) != len(binding_values):
            raise ValueError("objective selector binding values must be unique")
        if bool(self.bindings) is not bool(self.binding_source):
            raise ValueError("objective selector bindings require one authoritative source")
        expected = self.expected_binding_values()
        if self.bindings and set(binding_values) - set(expected):
            raise ValueError("objective selector bindings must match sealed selector scope")
        return self

    def expected_binding_values(self) -> tuple[str, ...]:
        if self.quantifier is ObjectiveSelectorQuantifier.ALL_AVAILABLE:
            if self.scope_values:
                return self.scope_values
            if self.scope_dimension:
                return tuple(binding.selector_value for binding in self.bindings)
            return ("*",)
        return self.values

    def has_finite_denominator(self) -> bool:
        if self.binding_source is None:
            return False
        expected = set(self.expected_binding_values())
        bound = {
            binding.selector_value
            for binding in self.bindings
            if binding.status is ObjectiveSelectorBindingStatus.BOUND
        }
        return bool(expected) and bound == expected


class ObjectiveStatus(StrEnum):
    UNMET = "unmet"
    ACTIVE = "active"
    SATISFIED_BY_CAMPAIGN = "satisfied_by_campaign"
    PREEXISTING = "preexisting"
    BLOCKED = "blocked"
    WAIVED = "waived"
    INDETERMINATE = "indeterminate"


class ObjectiveProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ObjectiveProvenanceKind
    source_ref: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class CampaignObjective(BaseModel):
    """One durable, target-neutral objective sealed into a campaign."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.campaign-objective.v1"] = (
        CAMPAIGN_OBJECTIVE_SCHEMA_VERSION
    )
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: ObjectiveKind
    temporal_semantics: ObjectiveTemporalSemantics
    provenance: ObjectiveProvenance
    required: bool = True
    depends_on: tuple[str, ...] = ()
    selectors: tuple[ObjectiveSelector, ...] = ()
    source_span: ObjectiveSourceSpan | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> CampaignObjective:
        if self.id in self.depends_on:
            raise ValueError("an objective cannot depend on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("objective dependencies must be unique")
        return self


class CampaignObjectiveContract(BaseModel):
    """Operator-authored requirements sealed with their exact source and typed selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.campaign-objective-contract.v1"] = (
        CAMPAIGN_OBJECTIVE_CONTRACT_SCHEMA_VERSION
    )
    source_text: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    objectives: tuple[CampaignObjective, ...]

    @model_validator(mode="after")
    def validate_contract(self) -> CampaignObjectiveContract:
        ids = tuple(item.id for item in self.objectives)
        if len(set(ids)) != len(ids):
            raise ValueError("objective contract ids must be unique")
        known = set(ids)
        for objective in self.objectives:
            unknown = set(objective.depends_on) - known
            if unknown:
                raise ValueError(
                    f"objective {objective.id} has unknown dependencies: "
                    f"{', '.join(sorted(unknown))}"
                )
            if objective.source_span is not None:
                span = objective.source_span
                if self.source_text[span.start : span.end] != span.text:
                    raise ValueError("objective source span does not match contract source text")
            for selector in objective.selectors:
                span = selector.source_span
                if self.source_text[span.start : span.end] != span.text:
                    raise ValueError("objective source span does not match contract source text")
        return self


class ObjectiveContractAmbiguity(ValueError):
    """Explicit requirement syntax was recognized but cannot be sealed safely."""


_RANGE_PATTERN = re.compile(
    r"\b(?P<label>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<start>\d+)\s*"
    r"(?:through|to|[-–])\s*(?:(?P=label)\s+)?(?P<end>\d+)\b",
    re.IGNORECASE,
)
_ENUMERATED_RANGE_PATTERN = re.compile(
    r"\b(?P<label>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<start>\d+)"
    r"(?P<middle>(?:\s*,\s*(?P=label)\s+\d+)+)"
    r"\s*,?\s*(?:and\s+)?(?P=label)\s+(?P<end>\d+)\b",
    re.IGNORECASE,
)
_ALL_AVAILABLE_PATTERN = re.compile(
    r"\b(?P<quantifier>every(?:\s+available)?|all\s+available)\s+"
    r"(?P<dimension>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?:\s+in\s+each\s+(?P<scope>[A-Za-z][A-Za-z0-9_-]*))?\b",
    re.IGNORECASE,
)
_EXPLICIT_LIST_PATTERN = re.compile(
    r"(?:^|(?<=[.!?\n]))[ \t]*(?:then[ \t]+)?"
    r"(?:try|test|attempt|attempted|exercise|assess)[ \t]+"
    r"(?P<items>[^.!?\n]{1,300}?)"
    r"(?=[ \t]+(?:to|that[ \t]+(?:attempts?|tries|seeks?)[ \t]+to)"
    r"[ \t]+[A-Za-z]|[.!?\n]|$)",
    re.IGNORECASE,
)
_EXPLICIT_LIST_ITEM = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_-]*){1,4}"
)
def _singular(value: str) -> str:
    lowered = value.casefold().replace("-", "_")
    if lowered.endswith("ies") and len(lowered) > 3:
        return lowered[:-3] + "y"
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _objective_id(prefix: str, span: ObjectiveSourceSpan) -> str:
    digest = hashlib.sha256(f"{span.start}:{span.end}:{span.text}".encode()).hexdigest()[:12]
    return f"operator/{prefix}/{digest}"


def compile_objective_contract(
    source_text: str,
    *,
    source_ref: str = "operator:campaign-request",
) -> CampaignObjectiveContract:
    """Compile common explicit coverage requirements without model interpretation.

    The complete operator text is always sealed.  This compiler only emits typed
    objectives for syntax it can preserve exactly: inclusive numbered ranges and
    ``all/every available`` selectors.  Target adapters resolve open-world selectors
    later; this function never invents target-specific values or oracles.
    """

    text = source_text.strip()
    if not text:
        raise ValueError("objective contract source text cannot be empty")
    objectives: list[CampaignObjective] = []
    ranges: dict[str, tuple[str, ...]] = {}
    occupied: list[tuple[int, int]] = []

    range_matches = [*_RANGE_PATTERN.finditer(text), *_ENUMERATED_RANGE_PATTERN.finditer(text)]
    for match in sorted(range_matches, key=lambda item: item.start()):
        start = int(match.group("start"))
        end = int(match.group("end"))
        if match.re is _ENUMERATED_RANGE_PATTERN:
            observed = (start, *map(int, re.findall(r"\d+", match.group("middle"))), end)
            expected = tuple(range(start, end + 1))
            if observed != expected:
                raise ObjectiveContractAmbiguity(
                    f"enumerated range {match.group(0)!r} is not contiguous"
                )
        if end < start:
            raise ObjectiveContractAmbiguity(
                f"descending range {match.group(0)!r} is ambiguous; state the required order"
            )
        if end - start > 10_000:
            raise ObjectiveContractAmbiguity(
                f"range {match.group(0)!r} is too large to seal safely"
            )
        span = ObjectiveSourceSpan(
            start=match.start(), end=match.end(), text=match.group(0)
        )
        dimension = _singular(match.group("label"))
        values = tuple(str(item) for item in range(start, end + 1))
        ranges[dimension] = values
        occupied.append((span.start, span.end))
        objectives.append(
            CampaignObjective(
                id=_objective_id(f"cover-{dimension}-range", span),
                description=f"Cover every {dimension} from {start} through {end}",
                kind=ObjectiveKind.COVERAGE,
                temporal_semantics=ObjectiveTemporalSemantics.CUMULATIVE,
                provenance=ObjectiveProvenance(kind="operator", source_ref=source_ref),
                source_span=span,
                selectors=(
                    ObjectiveSelector(
                        dimension=dimension,
                        quantifier=ObjectiveSelectorQuantifier.INCLUSIVE_RANGE,
                        resolution=ObjectiveSelectorResolution.SEALED,
                        values=values,
                        source_span=span,
                    ),
                ),
            )
        )

    for match in _ALL_AVAILABLE_PATTERN.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in occupied):
            continue
        span = ObjectiveSourceSpan(
            start=match.start(), end=match.end(), text=match.group(0)
        )
        dimension = _singular(match.group("dimension"))
        scope = _singular(match.group("scope")) if match.group("scope") else None
        scope_values = ranges.get(scope or "", ())
        objectives.append(
            CampaignObjective(
                id=_objective_id(f"cover-all-{dimension}", span),
                description=(
                    f"Cover every available {dimension}"
                    + (f" in each {scope}" if scope else "")
                ),
                kind=ObjectiveKind.COVERAGE,
                temporal_semantics=ObjectiveTemporalSemantics.CUMULATIVE,
                provenance=ObjectiveProvenance(kind="operator", source_ref=source_ref),
                # The operator requirement remains a completion gate. A reviewed adapter
                # or authoritative discovery must bind its finite denominator later.
                required=True,
                source_span=span,
                selectors=(
                    ObjectiveSelector(
                        dimension=dimension,
                        quantifier=ObjectiveSelectorQuantifier.ALL_AVAILABLE,
                        resolution=ObjectiveSelectorResolution.ADAPTER_REQUIRED,
                        scope_dimension=scope,
                        scope_values=scope_values,
                        source_span=span,
                    ),
                ),
            )
        )

    for match in _EXPLICIT_LIST_PATTERN.finditer(text):
        raw_items = match.group("items").strip()
        raw_items = re.sub(r"[ \t]+in[ \t]+order$", "", raw_items, flags=re.IGNORECASE)
        # Require an explicit final conjunction. Comma-separated lists retain the
        # serial-comma guard; two-item lists must contain exactly one conjunction.
        # This excludes ordinary workflow prose such as "Assess the app,
        # authenticate, learn rules" and avoids guessing where clauses begin.
        if "," in raw_items:
            if not re.search(r",[ \t]+(?:and|or)[ \t]+", raw_items, re.IGNORECASE):
                continue
            parts = [part.strip() for part in raw_items.split(",")]
            if len(parts) < 3:
                continue
            parts[-1] = re.sub(
                r"^(?:and|or)[ \t]+", "", parts[-1], flags=re.IGNORECASE
            )
        else:
            parts = [
                part.strip()
                for part in re.split(r"[ \t]+(?:and|or)[ \t]+", raw_items, flags=re.IGNORECASE)
            ]
            if len(parts) != 2:
                continue
        if any(
            _EXPLICIT_LIST_ITEM.fullmatch(part) is None
            or part.split(maxsplit=1)[0].casefold() in {"a", "an", "the"}
            for part in parts
        ):
            continue
        span_start = match.start("items") + len(match.group("items")) - len(
            match.group("items").lstrip()
        )
        span = ObjectiveSourceSpan(
            start=span_start,
            end=span_start + len(raw_items),
            text=raw_items,
        )
        objectives.append(
            CampaignObjective(
                id=_objective_id("exercise-listed-methods", span),
                description="Exercise every explicitly listed method",
                kind=ObjectiveKind.BEHAVIOR,
                temporal_semantics=ObjectiveTemporalSemantics.CUMULATIVE,
                provenance=ObjectiveProvenance(kind="operator", source_ref=source_ref),
                source_span=span,
                selectors=(
                    ObjectiveSelector(
                        dimension="method",
                        quantifier=ObjectiveSelectorQuantifier.ALL_LISTED,
                        resolution=ObjectiveSelectorResolution.SEALED,
                        values=tuple(parts),
                        source_span=span,
                    ),
                ),
            )
        )

    # Free-form prose is intentionally not converted into a completion gate.  The
    # exact operator text remains the digest-bound manifest objective; typed gates
    # are limited to syntax this compiler can preserve without inventing clauses.
    return CampaignObjectiveContract(
        source_text=text,
        source_ref=source_ref,
        objectives=tuple(objectives),
    )


def resolve_all_available_selector(
    selector: ObjectiveSelector,
    *,
    candidate_ids_by_scope: Mapping[str, tuple[str, ...]],
    source: ObjectiveSelectorBindingSource,
) -> ObjectiveSelector:
    """Bind an open-world selector to a finite, authoritative denominator."""

    if selector.quantifier is not ObjectiveSelectorQuantifier.ALL_AVAILABLE:
        raise ValueError("only all_available selectors require denominator resolution")
    supplied = set(candidate_ids_by_scope)
    if selector.scope_values:
        expected = set(selector.scope_values)
    elif selector.scope_dimension:
        if not supplied:
            raise ValueError(
                "scoped all_available selectors require at least one discovered scope; "
                "use '*' with an empty denominator when authoritative discovery proves none"
            )
        expected = supplied
    else:
        expected = {"*"}
    if supplied != expected:
        missing = sorted(expected - supplied)
        unexpected = sorted(supplied - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing scopes: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected scopes: {', '.join(unexpected)}")
        raise ValueError("selector denominator does not match sealed scope; " + "; ".join(details))
    bindings = tuple(
        ObjectiveSelectorBinding(
            selector_value=value,
            candidate_ids=tuple(candidate_ids_by_scope[value]),
            status=ObjectiveSelectorBindingStatus.BOUND,
            evidence_refs=source.evidence_refs,
        )
        for value in candidate_ids_by_scope
    )
    return ObjectiveSelector.model_validate(
        {
            **selector.model_dump(),
            "bindings": bindings,
            "binding_source": source,
        }
    )


def unresolved_selector_dimensions(objective: CampaignObjective) -> tuple[str, ...]:
    """Return open-world dimensions that still lack a finite denominator."""

    return tuple(
        selector.dimension
        for selector in objective.selectors
        if selector.quantifier is ObjectiveSelectorQuantifier.ALL_AVAILABLE
        and not selector.has_finite_denominator()
    )


class ObjectiveProgress(BaseModel):
    """Current evidence-backed state of one sealed objective."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: CampaignObjective
    status: ObjectiveStatus = ObjectiveStatus.UNMET
    evidence_refs: tuple[str, ...] = ()
    status_reason: str | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> ObjectiveProgress:
        unresolved_dimensions = unresolved_selector_dimensions(self.objective)
        if self.status in {
            ObjectiveStatus.SATISFIED_BY_CAMPAIGN,
            ObjectiveStatus.PREEXISTING,
        } and unresolved_dimensions:
            raise ValueError(
                "objective cannot be discharged before all_available selectors have a "
                f"finite denominator: {', '.join(unresolved_dimensions)}"
            )
        if self.status in {
            ObjectiveStatus.SATISFIED_BY_CAMPAIGN,
            ObjectiveStatus.PREEXISTING,
            ObjectiveStatus.BLOCKED,
            ObjectiveStatus.WAIVED,
        } and not self.evidence_refs:
            raise ValueError(f"{self.status.value} objective status requires evidence")
        if self.status in {ObjectiveStatus.BLOCKED, ObjectiveStatus.WAIVED} and not (
            self.status_reason and self.status_reason.strip()
        ):
            raise ValueError(f"{self.status.value} objective status requires a reason")
        return self


class CampaignLifecycle(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    STOPPED = "stopped"
    FAILED = "failed"


class CampaignVerdict(StrEnum):
    SATISFIED = "satisfied"
    SATISFIED_WITH_WAIVERS = "satisfied_with_waivers"
    PARTIAL = "partial"
    NONE = "none"


class CampaignEndReason(StrEnum):
    ALL_SATISFIED = "all_satisfied"
    PROFILE_EXHAUSTED = "profile_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FRONTIER_EXHAUSTED = "frontier_exhausted"
    TARGET_TERMINAL = "target_terminal"
    BLOCKED_OBJECTIVES = "blocked_objectives"
    OPERATOR_STOPPED = "operator_stopped"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


class CampaignOutcomeProjection(BaseModel):
    """Closed lifecycle/verdict/reason projection derived from objective progress."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["merlin.campaign-outcome.v1"] = CAMPAIGN_OUTCOME_SCHEMA_VERSION
    lifecycle: CampaignLifecycle
    verdict: CampaignVerdict
    end_reason: CampaignEndReason | None = None
    required_objective_ids: tuple[str, ...]
    unresolved_objective_ids: tuple[str, ...]
    satisfied_by_campaign_ids: tuple[str, ...]
    preexisting_objective_ids: tuple[str, ...]
    waived_objective_ids: tuple[str, ...]


_DISCHARGED_STATUSES = frozenset(
    {
        ObjectiveStatus.SATISFIED_BY_CAMPAIGN,
        ObjectiveStatus.PREEXISTING,
        ObjectiveStatus.WAIVED,
    }
)


def project_campaign_outcome(
    objectives: tuple[ObjectiveProgress, ...],
    *,
    requested_end_reason: CampaignEndReason | None = None,
    active_lifecycle: CampaignLifecycle = CampaignLifecycle.RUNNING,
) -> CampaignOutcomeProjection:
    """Project an outcome while preventing search limits from claiming objective success.

    Search-policy exhaustion (profile, budget, or frontier) is operational evidence only.
    It cannot discharge an objective and therefore cannot produce a successful verdict while
    any required objective remains unresolved.
    """

    ids = tuple(item.objective.id for item in objectives)
    if len(set(ids)) != len(ids):
        raise ValueError("campaign objective ids must be unique")
    known_ids = set(ids)
    for item in objectives:
        unknown_dependencies = set(item.objective.depends_on) - known_ids
        if unknown_dependencies:
            raise ValueError(
                f"objective {item.objective.id} has unknown dependencies: "
                f"{', '.join(sorted(unknown_dependencies))}"
            )

    required = tuple(item for item in objectives if item.objective.required)
    unresolved = tuple(item for item in required if item.status not in _DISCHARGED_STATUSES)
    campaign_satisfied = tuple(
        item
        for item in required
        if item.status is ObjectiveStatus.SATISFIED_BY_CAMPAIGN
    )
    preexisting = tuple(
        item for item in required if item.status is ObjectiveStatus.PREEXISTING
    )
    waived = tuple(item for item in required if item.status is ObjectiveStatus.WAIVED)

    common = {
        "required_objective_ids": tuple(item.objective.id for item in required),
        "unresolved_objective_ids": tuple(item.objective.id for item in unresolved),
        "satisfied_by_campaign_ids": tuple(item.objective.id for item in campaign_satisfied),
        "preexisting_objective_ids": tuple(item.objective.id for item in preexisting),
        "waived_objective_ids": tuple(item.objective.id for item in waived),
    }

    if requested_end_reason is None:
        if active_lifecycle not in {
            CampaignLifecycle.PREPARED,
            CampaignLifecycle.RUNNING,
            CampaignLifecycle.PAUSED,
        }:
            raise ValueError("an active projection requires a nonterminal lifecycle")
        verdict = CampaignVerdict.PARTIAL if campaign_satisfied else CampaignVerdict.NONE
        return CampaignOutcomeProjection(
            lifecycle=active_lifecycle,
            verdict=verdict,
            **common,
        )

    if requested_end_reason is CampaignEndReason.ALL_SATISFIED:
        if not required:
            raise ValueError("all_satisfied requires at least one required objective")
        if unresolved:
            raise ValueError("all_satisfied is invalid while required objectives remain unresolved")
        verdict = (
            CampaignVerdict.SATISFIED_WITH_WAIVERS
            if waived
            else CampaignVerdict.SATISFIED
        )
        return CampaignOutcomeProjection(
            lifecycle=CampaignLifecycle.COMPLETED,
            verdict=verdict,
            end_reason=requested_end_reason,
            **common,
        )

    verdict = CampaignVerdict.PARTIAL if campaign_satisfied else CampaignVerdict.NONE
    if requested_end_reason is CampaignEndReason.OPERATOR_STOPPED:
        lifecycle = CampaignLifecycle.STOPPED
    elif requested_end_reason is CampaignEndReason.INFRASTRUCTURE_FAILURE:
        lifecycle = CampaignLifecycle.FAILED
    else:
        lifecycle = CampaignLifecycle.INCOMPLETE
    return CampaignOutcomeProjection(
        lifecycle=lifecycle,
        verdict=verdict,
        end_reason=requested_end_reason,
        **common,
    )
