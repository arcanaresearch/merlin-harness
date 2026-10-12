"""Generic executable constraints and state capabilities for reviewed targets."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type JsonScalar = str | int | float | bool | None


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


class StateSelector(TargetCapabilityContract):
    """One unambiguous value selector bound to an exact adapter step and root."""

    step_id: str = Field(min_length=1)
    evaluation_root: Literal["response_body", "captures"]
    pointer: str = Field(pattern=r"^(?:|/.*)$")


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
        return self


class StateAffordanceKind(StrEnum):
    PROVISION_STATE = "provision_state"
    PROVISION_FRESH_STATE = "provision_fresh_state"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    RESET_STATE = "reset_state"


class StateAffordance(TargetCapabilityContract):
    """A reviewed state-management operation available to the runtime."""

    kind: StateAffordanceKind
    step_id: str = Field(min_length=1)
    evaluation_root: Literal["response_body", "captures"]
    evidence_refs: tuple[str, ...] = ()


class StateFreshnessRule(TargetCapabilityContract):
    """A reviewed predicate proving the state observed at one exact step is fresh."""

    selector: StateSelector
    match_values: tuple[JsonScalar, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class StateCapabilities(TargetCapabilityContract):
    """Composable state facts; capability presence drives execution strategy."""

    identity: StateIdentity | None = None
    observations: tuple[StateObservation, ...] = ()
    freshness_rules: tuple[StateFreshnessRule, ...] = ()
    affordances: tuple[StateAffordance, ...] = ()
    maximum_parallel_states: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Self:
        observation_names = [item.name for item in self.observations]
        if len(observation_names) != len(set(observation_names)):
            raise ValueError("state observation names must be unique")
        affordance_kinds = [item.kind for item in self.affordances]
        if len(affordance_kinds) != len(set(affordance_kinds)):
            raise ValueError("state affordance kinds must be unique")
        if StateAffordanceKind.PROVISION_FRESH_STATE in affordance_kinds and (
            self.identity is None or not self.freshness_rules
        ):
            raise ValueError(
                "provision_fresh_state requires physical identity and a freshness rule"
            )
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
    if capabilities.identity is not None:
        result["identity"] = {
            "identity_scope": capabilities.identity.identity_scope,
            "baseline_selector": capabilities.identity.baseline_selector.model_dump(mode="json"),
            **(
                {"result_selector": capabilities.identity.result_selector.model_dump(mode="json")}
                if capabilities.identity.result_selector is not None
                else {}
            ),
        }
    if capabilities.observations:
        result["observations"] = [
            {
                "name": item.name,
                "role": item.role.value,
                "baseline_selector": item.baseline_selector.model_dump(mode="json"),
                "result_selector": item.result_selector.model_dump(mode="json"),
                **(
                    {"preferred_direction": item.preferred_direction.value}
                    if item.preferred_direction is not None
                    else {}
                ),
                **({"success_values": list(item.success_values)} if item.success_values else {}),
                **({"failure_values": list(item.failure_values)} if item.failure_values else {}),
            }
            for item in capabilities.observations
        ]
    if capabilities.freshness_rules:
        result["freshness_rules"] = [
            {
                "selector": item.selector.model_dump(mode="json"),
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
    if capabilities.maximum_parallel_states is not None:
        result["maximum_parallel_states"] = capabilities.maximum_parallel_states
    return result
