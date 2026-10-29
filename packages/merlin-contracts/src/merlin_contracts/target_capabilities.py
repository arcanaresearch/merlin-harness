"""Generic executable constraints and state capabilities for reviewed targets."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type JsonScalar = str | int | float | bool | None

STATE_CAPABILITIES_SCHEMA_VERSION = "merlin.state-capabilities.v1"
STATE_SELECTOR_SCHEMA_VERSION = "merlin.state-selector.v1"
STATE_CHECKPOINT_SCHEMA_VERSION = "merlin.state-checkpoint.v1"
STATE_ENUMERATION_SCHEMA_VERSION = "merlin.state-enumeration-declaration.v1"


class TargetCapabilityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestConstraintKind(StrEnum):
    STRING_LENGTH = "string_length"


class StringLengthUnit(StrEnum):
    UNICODE_CHARACTERS = "unicode_characters"
    UTF8_BYTES = "utf8_bytes"


class EvaluationRoot(StrEnum):
    REQUEST_BODY = "request_body"
    RESPONSE_BODY = "response_body"
    CAPTURES = "captures"


class ConstraintEvidenceBinding(TargetCapabilityContract):
    evidence_ref: str = Field(min_length=1)
    observed_maximum: int = Field(ge=1)


class RequestConstraint(TargetCapabilityContract):
    """A reviewed constraint over the fully rendered target request."""

    kind: Literal["string_length"] = RequestConstraintKind.STRING_LENGTH.value
    step_id: str = Field(min_length=1)
    evaluation_root: Literal["request_body"] = EvaluationRoot.REQUEST_BODY.value
    path: str = Field(pattern=r"^(?:|/.*)$")
    maximum: int = Field(ge=1)
    unit: StringLengthUnit = StringLengthUnit.UNICODE_CHARACTERS
    source: str | None = Field(default=None, min_length=1)
    evidence_refs: tuple[str, ...] = ()
    evidence_bindings: tuple[ConstraintEvidenceBinding, ...] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_evidence_values(self) -> Self:
        if any(item.observed_maximum != self.maximum for item in self.evidence_bindings):
            raise ValueError("constraint evidence must bind the reviewed maximum")
        return self


class StructuredConstraintRejection(TargetCapabilityContract):
    """Reviewed mapping from a structured target rejection to a request constraint."""

    kind: Literal["string_length"] = RequestConstraintKind.STRING_LENGTH.value
    status_codes: tuple[int, ...] = Field(min_length=1)
    request_path: str = Field(pattern=r"^(?:|/.*)$")
    maximum_pointer: str = Field(pattern=r"^(?:|/.*)$")
    unit: StringLengthUnit = StringLengthUnit.UNICODE_CHARACTERS

    @model_validator(mode="after")
    def validate_status_codes(self) -> Self:
        if any(isinstance(value, bool) or not 400 <= value <= 599 for value in self.status_codes):
            raise ValueError("structured constraint rejection requires 4xx/5xx status codes")
        if len(set(self.status_codes)) != len(self.status_codes):
            raise ValueError("structured constraint rejection status codes must be unique")
        return self


class StateObservationRole(StrEnum):
    STATE_IDENTITY = "state_identity"
    PROGRESS_ORDINAL = "progress_ordinal"
    SCORE = "score"
    BUDGET_REMAINING = "budget_remaining"
    TERMINAL_STATUS = "terminal_status"
    CUSTOM = "custom"


class PreferredDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"


class StateValueType(StrEnum):
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"


class StateMonotonicity(StrEnum):
    NONDECREASING = "nondecreasing"
    NONINCREASING = "nonincreasing"


class ObservationTrustClass(StrEnum):
    TARGET_REPORTED = "target_reported"
    HOST_OBSERVED = "host_observed"
    INDEPENDENT_AUTHORITY = "independent_authority"


class StateSelector(TargetCapabilityContract):
    """One unambiguous value selector bound to an exact adapter step and root."""

    schema_version: Literal["merlin.state-selector.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    step_id: str = Field(min_length=1)
    evaluation_root: Literal["response_body", "captures"]
    pointer: str = Field(pattern=r"^(?:|/.*)$")
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class StateIdentity(TargetCapabilityContract):
    """Selectors used to attribute an attempt to one physical remote state."""

    identity_scope: Literal["physical_state"] = "physical_state"
    baseline_selector: StateSelector
    result_selector: StateSelector | None = None
    evidence_refs: tuple[str, ...] = ()


class StateObservation(TargetCapabilityContract):
    """A typed, target-authored value that can be compared between calls."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    role: StateObservationRole
    baseline_selector: StateSelector
    result_selector: StateSelector
    preferred_direction: PreferredDirection | None = None
    success_values: tuple[JsonScalar, ...] = ()
    failure_values: tuple[JsonScalar, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    value_type: StateValueType | None = None
    minimum_value: int | float | None = None
    maximum_value: int | float | None = None
    monotonicity: StateMonotonicity | None = None
    trust_class: ObservationTrustClass | None = None

    @model_validator(mode="after")
    def validate_role_semantics(self) -> Self:
        if self.preferred_direction is not None and self.role not in {
            StateObservationRole.PROGRESS_ORDINAL,
            StateObservationRole.SCORE,
            StateObservationRole.BUDGET_REMAINING,
            StateObservationRole.CUSTOM,
        }:
            raise ValueError("preferred_direction requires an ordered observation role")
        if (self.success_values or self.failure_values) and self.role not in {
            StateObservationRole.TERMINAL_STATUS,
            StateObservationRole.CUSTOM,
        }:
            raise ValueError("success_values and failure_values require a terminal or custom role")
        if set(self.success_values) & set(self.failure_values):
            raise ValueError("success_values and failure_values must not overlap")
        if self.minimum_value is not None or self.maximum_value is not None:
            if self.value_type not in {StateValueType.INTEGER, StateValueType.NUMBER}:
                raise ValueError("numeric bounds require an integer or number value_type")
            if self.minimum_value is not None and isinstance(self.minimum_value, bool):
                raise ValueError("numeric bounds cannot be booleans")
            if self.maximum_value is not None and isinstance(self.maximum_value, bool):
                raise ValueError("numeric bounds cannot be booleans")
            if (
                self.minimum_value is not None
                and self.maximum_value is not None
                and self.minimum_value > self.maximum_value
            ):
                raise ValueError("minimum_value cannot exceed maximum_value")
        if self.monotonicity is not None and self.value_type not in {
            StateValueType.INTEGER,
            StateValueType.NUMBER,
        }:
            raise ValueError("monotonicity requires an integer or number value_type")
        return self


class StateEnumerationShape(StrEnum):
    FLAT = "flat"
    SCOPED = "scoped"


class StateEnumerationCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class StateEnumerationDeclaration(TargetCapabilityContract):
    """Reviewed extraction of a finite target-authored member domain."""

    schema_version: Literal["merlin.state-enumeration-declaration.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    selector: StateSelector
    shape: StateEnumerationShape = StateEnumerationShape.FLAT
    scope_dimension: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    completeness: StateEnumerationCompleteness
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if (self.shape is StateEnumerationShape.SCOPED) != (self.scope_dimension is not None):
            raise ValueError("scoped enumerations require exactly one scope_dimension")
        return self


class StateCorroborationRule(TargetCapabilityContract):
    """Agreement required across separately declared observations."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    observation_names: tuple[str, ...] = Field(min_length=2)
    minimum_agreement: int = Field(ge=2)

    @model_validator(mode="after")
    def validate_observation_names(self) -> Self:
        if len(set(self.observation_names)) != len(self.observation_names):
            raise ValueError("corroboration observation names must be unique")
        if self.minimum_agreement > len(self.observation_names):
            raise ValueError("minimum_agreement cannot exceed the observation count")
        return self


class IdentityEquivalenceRule(TargetCapabilityContract):
    """Proves continuity when a target rotates its physical-state identifier.

    Equality of the selected linkage values binds the baseline and result to the
    same physical state within one adapter invocation.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    relation: Literal["equal"] = "equal"
    left_selector: StateSelector
    right_selector: StateSelector
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_distinct_bindings(self) -> Self:
        if (
            self.left_selector.step_id,
            self.left_selector.evaluation_root,
            self.left_selector.pointer,
        ) == (
            self.right_selector.step_id,
            self.right_selector.evaluation_root,
            self.right_selector.pointer,
        ):
            raise ValueError("identity equivalence requires two distinct selector bindings")
        return self


class StateResourceKind(StrEnum):
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    BUDGET = "budget"


class TargetModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    MIXED = "mixed"


class SupportedModality(TargetCapabilityContract):
    """A request modality demonstrated by one exact reviewed adapter step."""

    modality: TargetModality
    step_id: str = Field(min_length=1)
    request_pointer: str = Field(pattern=r"^(?:|/.*)$")
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ResourceOwnership(TargetCapabilityContract):
    """Binds a state observation to one principal at the adapter authority."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    resource_kind: StateResourceKind
    resource_observation_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    principal_selector: StateSelector
    authority: Literal["adapter_origin"] = "adapter_origin"
    reset_selector: StateSelector | None = None
    evidence_refs: tuple[str, ...] = ()


class StateAffordanceKind(StrEnum):
    PROVISION_STATE = "provision_state"
    PROVISION_FRESH_STATE = "provision_fresh_state"
    CLONE_STATE = "clone_state"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    RESET_STATE = "reset_state"
    CLEANUP_STATE = "cleanup_state"


class StateAffordance(TargetCapabilityContract):
    """A reviewed state-management operation available to the runtime."""

    kind: StateAffordanceKind
    step_id: str = Field(min_length=1)
    evaluation_root: Literal["response_body", "captures"]
    evidence_refs: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class StateLifecycleIsolation(StrEnum):
    INDEPENDENT = "independent"
    SERIALIZED = "serialized"
    UNKNOWN = "unknown"


class StateBaselineStrategy(StrEnum):
    FRESH = "fresh"
    CLONE = "clone"
    RESTORE = "restore"
    RESET = "reset"
    SHARED = "shared"
    UNKNOWN = "unknown"


class StateCleanupStrategy(StrEnum):
    EXPLICIT = "explicit"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class StateLifecycleClassification(TargetCapabilityContract):
    """Reviewed lifecycle semantics independent of target-specific operation names."""

    isolation: StateLifecycleIsolation
    baseline: StateBaselineStrategy
    cleanup: StateCleanupStrategy
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class StateFreshnessRule(TargetCapabilityContract):
    """A reviewed predicate proving the state observed at one exact step is fresh."""

    selector: StateSelector
    match_values: tuple[JsonScalar, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class StateCheckpoint(TargetCapabilityContract):
    """Reviewed extraction and injection contract for an opaque checkpoint handle."""

    schema_version: Literal["merlin.state-checkpoint.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    capture_selector: StateSelector
    capture_timing: Literal["post_success_response", "live_handle"]
    binding_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    restore_step_id: str = Field(min_length=1)
    restore_request_pointer: str = Field(pattern=r"^/(?:.*)$")
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_capture_evidence(self) -> Self:
        if self.capture_timing == "live_handle" and not self.evidence_refs:
            raise ValueError("live checkpoint handles require reviewed tracking evidence")
        return self


class StructuredResourceBlock(TargetCapabilityContract):
    """Reviewed mapping from a structured rejection to a resumable resource block."""

    status_codes: tuple[int, ...] = Field(min_length=1)
    retry_after_seconds_pointer: str | None = Field(default=None, pattern=r"^(?:|/.*)$")
    wake_at_pointer: str | None = Field(default=None, pattern=r"^(?:|/.*)$")
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_block_signal(self) -> Self:
        if any(isinstance(value, bool) or not 400 <= value <= 599 for value in self.status_codes):
            raise ValueError("structured resource block requires 4xx/5xx status codes")
        if len(set(self.status_codes)) != len(self.status_codes):
            raise ValueError("structured resource block status codes must be unique")
        if (self.retry_after_seconds_pointer is None) == (self.wake_at_pointer is None):
            raise ValueError("structured resource block requires exactly one wake-time pointer")
        return self


class ResourceControl(TargetCapabilityContract):
    """One principal-owned target resource reserved before an interaction."""

    ownership_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    step_id: str = Field(min_length=1)
    reservation_amount: int = Field(default=1, ge=1)
    limit: int = Field(ge=1)
    window_key: str = Field(min_length=1)
    structured_blocks: tuple[StructuredResourceBlock, ...] = Field(min_length=1)


class StateCapabilities(TargetCapabilityContract):
    """Composable state facts; capability presence drives execution strategy."""

    schema_version: Literal["merlin.state-capabilities.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    identity: StateIdentity | None = None
    observations: tuple[StateObservation, ...] = ()
    enumerations: tuple[StateEnumerationDeclaration, ...] = ()
    corroboration_rules: tuple[StateCorroborationRule, ...] = ()
    identity_equivalence_rules: tuple[IdentityEquivalenceRule, ...] = ()
    resource_ownership: tuple[ResourceOwnership, ...] = ()
    freshness_rules: tuple[StateFreshnessRule, ...] = ()
    affordances: tuple[StateAffordance, ...] = ()
    lifecycle: StateLifecycleClassification | None = None
    checkpoint: StateCheckpoint | None = None
    resource_controls: tuple[ResourceControl, ...] = ()
    supported_modalities: tuple[SupportedModality, ...] = ()
    maximum_parallel_states: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Self:
        selectors: list[StateSelector] = []
        if self.identity is not None:
            selectors.append(self.identity.baseline_selector)
            if self.identity.result_selector is not None:
                selectors.append(self.identity.result_selector)
        for observation in self.observations:
            selectors.extend((observation.baseline_selector, observation.result_selector))
        selectors.extend(item.selector for item in self.enumerations)
        for rule in self.identity_equivalence_rules:
            selectors.extend((rule.left_selector, rule.right_selector))
        for ownership in self.resource_ownership:
            selectors.append(ownership.principal_selector)
            if ownership.reset_selector is not None:
                selectors.append(ownership.reset_selector)
        selectors.extend(item.selector for item in self.freshness_rules)
        if self.checkpoint is not None:
            selectors.append(self.checkpoint.capture_selector)
        if self.schema_version is not None and any(
            selector.schema_version is None for selector in selectors
        ):
            raise ValueError("versioned state capabilities require every selector to be versioned")
        if self.schema_version is not None:
            incomplete_selectors = [
                selector
                for selector in selectors
                if not selector.evidence_refs or selector.confidence is None
            ]
            if incomplete_selectors:
                raise ValueError(
                    "versioned executable selectors require evidence references and confidence"
                )
            if self.affordances and self.lifecycle is None:
                raise ValueError("versioned state affordances require lifecycle classification")
            if any(
                not affordance.evidence_refs or affordance.confidence is None
                for affordance in self.affordances
            ):
                raise ValueError(
                    "versioned executable affordances require evidence references and confidence"
                )
            if any(
                enumeration.schema_version is None
                or not enumeration.evidence_refs
                or enumeration.confidence is None
                for enumeration in self.enumerations
            ):
                raise ValueError(
                    "versioned state enumerations require versioned declarations, evidence "
                    "references, and confidence"
                )
        if (
            self.schema_version is not None
            and self.checkpoint is not None
            and self.checkpoint.schema_version is None
        ):
            raise ValueError("versioned state capabilities require a versioned checkpoint")
        observation_names = [item.name for item in self.observations]
        if len(observation_names) != len(set(observation_names)):
            raise ValueError("state observation names must be unique")
        observation_name_set = set(observation_names)
        enumeration_names = [item.name for item in self.enumerations]
        if len(enumeration_names) != len(set(enumeration_names)):
            raise ValueError("state enumeration names must be unique")
        rule_names = [item.name for item in self.corroboration_rules]
        if len(rule_names) != len(set(rule_names)):
            raise ValueError("state corroboration rule names must be unique")
        for rule in self.corroboration_rules:
            unknown = sorted(set(rule.observation_names) - observation_name_set)
            if unknown:
                raise ValueError(
                    "corroboration rules reference unknown observations: " + ", ".join(unknown)
                )
        equivalence_names = [item.name for item in self.identity_equivalence_rules]
        if len(equivalence_names) != len(set(equivalence_names)):
            raise ValueError("identity equivalence rule names must be unique")
        ownership_names = [item.name for item in self.resource_ownership]
        if len(ownership_names) != len(set(ownership_names)):
            raise ValueError("resource ownership names must be unique")
        for ownership in self.resource_ownership:
            if ownership.resource_observation_name not in observation_name_set:
                raise ValueError(
                    "resource ownership references unknown observation: "
                    + ownership.resource_observation_name
                )
        affordance_kinds = [item.kind for item in self.affordances]
        if len(affordance_kinds) != len(set(affordance_kinds)):
            raise ValueError("state affordance kinds must be unique")
        resource_keys = [(item.ownership_name, item.step_id) for item in self.resource_controls]
        if len(resource_keys) != len(set(resource_keys)):
            raise ValueError("resource controls must be unique by ownership and step")
        unknown_control_owners = sorted(
            {item.ownership_name for item in self.resource_controls} - set(ownership_names)
        )
        if unknown_control_owners:
            raise ValueError(
                "resource controls reference unknown ownership: "
                + ", ".join(unknown_control_owners)
            )
        modality_keys = [
            (item.modality, item.step_id, item.request_pointer)
            for item in self.supported_modalities
        ]
        if len(modality_keys) != len(set(modality_keys)):
            raise ValueError("supported modalities must be unique by modality, step, and pointer")
        if StateAffordanceKind.PROVISION_FRESH_STATE in affordance_kinds and (
            self.identity is None or not self.freshness_rules
        ):
            raise ValueError(
                "provision_fresh_state requires physical identity and a freshness rule"
            )
        has_restore_affordance = StateAffordanceKind.RESTORE_CHECKPOINT in affordance_kinds
        if has_restore_affordance != (self.checkpoint is not None):
            raise ValueError(
                "restore_checkpoint affordance and checkpoint binding must be declared together"
            )
        if self.lifecycle is not None:
            baseline_kind = {
                StateBaselineStrategy.FRESH: StateAffordanceKind.PROVISION_FRESH_STATE,
                StateBaselineStrategy.CLONE: StateAffordanceKind.CLONE_STATE,
                StateBaselineStrategy.RESTORE: StateAffordanceKind.RESTORE_CHECKPOINT,
                StateBaselineStrategy.RESET: StateAffordanceKind.RESET_STATE,
            }.get(self.lifecycle.baseline)
            if baseline_kind is not None and baseline_kind not in affordance_kinds:
                raise ValueError(
                    f"{self.lifecycle.baseline.value} lifecycle requires its reviewed affordance"
                )
            if (
                self.lifecycle.cleanup is StateCleanupStrategy.EXPLICIT
                and StateAffordanceKind.CLEANUP_STATE not in affordance_kinds
            ):
                raise ValueError("explicit cleanup lifecycle requires cleanup_state affordance")
            if (
                self.lifecycle.isolation is StateLifecycleIsolation.INDEPENDENT
                and self.lifecycle.baseline
                not in {
                    StateBaselineStrategy.FRESH,
                    StateBaselineStrategy.CLONE,
                    StateBaselineStrategy.RESTORE,
                }
            ):
                raise ValueError("independent lifecycle requires fresh, clone, or restore baseline")
        return self


def request_constraints_semantic_projection(
    constraints: tuple[RequestConstraint, ...],
) -> list[dict[str, object]]:
    """Return executable constraint semantics without evidence provenance."""
    return [
        {
            "kind": item.kind,
            "step_id": item.step_id,
            "evaluation_root": item.evaluation_root,
            "path": item.path,
            "maximum": item.maximum,
            "unit": item.unit.value,
        }
        for item in constraints
    ]


def state_capabilities_semantic_projection(
    capabilities: StateCapabilities,
) -> dict[str, object]:
    """Return executable state semantics without evidence provenance."""
    result: dict[str, object] = {}
    if capabilities.schema_version is not None:
        result["schema_version"] = capabilities.schema_version
    if capabilities.identity is not None:
        result["identity"] = {
            "identity_scope": capabilities.identity.identity_scope,
            "baseline_selector": capabilities.identity.baseline_selector.model_dump(
                mode="json", exclude={"evidence_refs", "confidence"}
            ),
            **(
                {
                    "result_selector": capabilities.identity.result_selector.model_dump(
                        mode="json", exclude={"evidence_refs", "confidence"}
                    )
                }
                if capabilities.identity.result_selector is not None
                else {}
            ),
        }
    if capabilities.observations:
        result["observations"] = [
            {
                "name": item.name,
                "role": item.role.value,
                "baseline_selector": item.baseline_selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                "result_selector": item.result_selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                **(
                    {"preferred_direction": item.preferred_direction.value}
                    if item.preferred_direction is not None
                    else {}
                ),
                **({"success_values": list(item.success_values)} if item.success_values else {}),
                **({"failure_values": list(item.failure_values)} if item.failure_values else {}),
                **({"value_type": item.value_type.value} if item.value_type is not None else {}),
                **({"minimum_value": item.minimum_value} if item.minimum_value is not None else {}),
                **({"maximum_value": item.maximum_value} if item.maximum_value is not None else {}),
                **(
                    {"monotonicity": item.monotonicity.value}
                    if item.monotonicity is not None
                    else {}
                ),
                **({"trust_class": item.trust_class.value} if item.trust_class is not None else {}),
            }
            for item in capabilities.observations
        ]
    if capabilities.enumerations:
        result["enumerations"] = [
            {
                **(
                    {"schema_version": item.schema_version}
                    if item.schema_version is not None
                    else {}
                ),
                "name": item.name,
                "selector": item.selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                "shape": item.shape.value,
                **(
                    {"scope_dimension": item.scope_dimension}
                    if item.scope_dimension is not None
                    else {}
                ),
                "completeness": item.completeness.value,
            }
            for item in capabilities.enumerations
        ]
    if capabilities.corroboration_rules:
        result["corroboration_rules"] = [
            {
                "name": item.name,
                "observation_names": list(item.observation_names),
                "minimum_agreement": item.minimum_agreement,
            }
            for item in capabilities.corroboration_rules
        ]
    if capabilities.identity_equivalence_rules:
        result["identity_equivalence_rules"] = [
            {
                "name": item.name,
                "relation": item.relation,
                "left_selector": item.left_selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                "right_selector": item.right_selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
            }
            for item in capabilities.identity_equivalence_rules
        ]
    if capabilities.resource_ownership:
        result["resource_ownership"] = [
            {
                "name": item.name,
                "resource_kind": item.resource_kind.value,
                "resource_observation_name": item.resource_observation_name,
                "principal_selector": item.principal_selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                "authority": item.authority,
                **(
                    {
                        "reset_selector": item.reset_selector.model_dump(
                            mode="json", exclude={"evidence_refs", "confidence"}
                        )
                    }
                    if item.reset_selector is not None
                    else {}
                ),
            }
            for item in capabilities.resource_ownership
        ]
    if capabilities.freshness_rules:
        result["freshness_rules"] = [
            {
                "selector": item.selector.model_dump(
                    mode="json", exclude={"evidence_refs", "confidence"}
                ),
                "match_values": list(item.match_values),
            }
            for item in capabilities.freshness_rules
        ]
    if capabilities.affordances:
        result["affordances"] = [
            {
                "kind": item.kind.value,
                "step_id": item.step_id,
                "evaluation_root": item.evaluation_root,
            }
            for item in capabilities.affordances
        ]
    if capabilities.lifecycle is not None:
        result["lifecycle"] = {
            "isolation": capabilities.lifecycle.isolation.value,
            "baseline": capabilities.lifecycle.baseline.value,
            "cleanup": capabilities.lifecycle.cleanup.value,
        }
    if capabilities.checkpoint is not None:
        checkpoint = capabilities.checkpoint
        result["checkpoint"] = {
            **(
                {"schema_version": checkpoint.schema_version}
                if checkpoint.schema_version is not None
                else {}
            ),
            "capture_selector": checkpoint.capture_selector.model_dump(
                mode="json", exclude={"evidence_refs", "confidence"}
            ),
            "capture_timing": checkpoint.capture_timing,
            "binding_name": checkpoint.binding_name,
            "restore_step_id": checkpoint.restore_step_id,
            "restore_request_pointer": checkpoint.restore_request_pointer,
        }
    if capabilities.resource_controls:
        result["resource_controls"] = [
            {
                "ownership_name": item.ownership_name,
                "step_id": item.step_id,
                "reservation_amount": item.reservation_amount,
                "limit": item.limit,
                "window_key": item.window_key,
                "structured_blocks": [
                    block.model_dump(mode="json", exclude={"evidence_refs"})
                    for block in item.structured_blocks
                ],
            }
            for item in capabilities.resource_controls
        ]
    if capabilities.supported_modalities:
        result["supported_modalities"] = [
            {
                "modality": item.modality.value,
                "step_id": item.step_id,
                "request_pointer": item.request_pointer,
            }
            for item in capabilities.supported_modalities
        ]
    if capabilities.maximum_parallel_states is not None:
        result["maximum_parallel_states"] = capabilities.maximum_parallel_states
    return result
