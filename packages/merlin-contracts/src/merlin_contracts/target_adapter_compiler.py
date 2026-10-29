"""Compile legacy target-adapter mappings into one executable contract shape.

This module is deliberately additive.  It does not replace the frozen v1
projection in :mod:`merlin_contracts.target_adapter`; downstream consumers can
adopt the compiler independently without changing persisted v1 digests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from functools import wraps
from typing import Annotated, ClassVar, Literal, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .request_bindings import (
    RequestValueGenerator,
    validate_capture_binding,
    validate_input_binding,
    validate_request_bindings,
)
from .target_capabilities import (
    RequestConstraint,
    StateCapabilities,
    StructuredConstraintRejection,
    request_constraints_semantic_projection,
    state_capabilities_semantic_projection,
)

COMPILED_TARGET_ADAPTER_VERSION = "merlin.compiled-target-adapter.v1"
ADAPTER_COMPILATION_VERSION = "merlin.target-adapter-compilation.v1"
BROWSER_STEP_SOURCE_INDEX_KEY = "__merlin_source_index"
BROWSER_TEXT_CAPTURE_MAX_LENGTH = 512
BROWSER_STATE_OBSERVATION_LIMIT = 32
BROWSER_STATE_NAME_MAX_LENGTH = 64
BROWSER_STATE_SCALAR_LIMIT = 32
BROWSER_STATE_SCALAR_STRING_MAX_LENGTH = 512
BROWSER_STATE_EVIDENCE_LIMIT = 32
BROWSER_STATE_EVIDENCE_MAX_LENGTH = 512
BROWSER_ADVISORY_PREVIEW_MAX_LENGTH = 2_048


class CompiledAdapterKind(StrEnum):
    PROVIDER_CHAT = "provider_chat"
    HTTP_API = "http_api"
    BROWSER_CHAT = "browser_chat"


class TargetActor(StrEnum):
    TARGET_CHAT = "target_chat"
    TARGET_REQUEST = "target_request"
    BROWSER_CHAT_EXCHANGE = "browser_chat_exchange"


class TargetDelivery(StrEnum):
    DIRECT_USER_CHANNEL = "direct-user-channel"
    INTER_AGENT_MESSAGE = "inter-agent-message"


class ApiTransport(StrEnum):
    DIRECT = "direct"
    BROWSER_CONTEXT = "browser_context"


class DeliveryResolution(StrEnum):
    RESOLVED = "resolved"
    UNSPECIFIED = "unspecified"
    UNRESOLVED = "unresolved"


class CleanupLifecycle(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class AdapterStepRole(StrEnum):
    SETUP = "setup"
    INTERACTION = "interaction"
    CLEANUP = "cleanup"


class AdapterAdvisoryKind(StrEnum):
    EVALUATION_HINT = "evaluation_hint"
    OBSERVATION = "observation"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


class AdapterCompileFailureKind(StrEnum):
    MALFORMED_CONTRACT = "malformed_contract"
    UNKNOWN_INTERFACE = "unknown_interface"
    NON_EXECUTABLE_ENDPOINT = "non_executable_endpoint"
    UNAUTHORIZED_ORIGIN = "unauthorized_origin"
    NON_EXECUTABLE_INPUT = "non_executable_input"
    NON_EXECUTABLE_TRANSPORT = "non_executable_transport"
    NON_EXECUTABLE_CLEANUP = "non_executable_cleanup"
    NON_EXECUTABLE_STEPS = "non_executable_steps"
    MALFORMED_CREDENTIAL = "malformed_credential"
    CONTRACT_VALIDATION = "contract_validation"


class AdapterCompileError(ValueError):
    """A typed hard failure for a contract that cannot perform a target call."""

    def __init__(self, kind: AdapterCompileFailureKind, path: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.path = path


class AdapterContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdapterAdvisory(AdapterContract):
    kind: AdapterAdvisoryKind
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)
    value: object | None = None


class HttpApiStep(AdapterContract):
    kind: Literal["http_api"] = "http_api"
    role: AdapterStepRole
    step_id: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    endpoint: str = Field(min_length=1)
    method: str = Field(min_length=1)
    request_template: object
    request_bindings: dict[str, RequestValueGenerator] = Field(default_factory=dict)
    captures: dict[str, str] = Field(default_factory=dict)
    repeat: int = Field(default=1, ge=1)
    idempotency_key: str | None = None
    replay_after_ambiguous_result: bool = False
    structured_constraint_rejections: tuple[StructuredConstraintRejection, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        validate_request_bindings(self.request_template, self.request_bindings)
        return self


class ContentInjection(AdapterContract):
    """Reviewed surface where attacker-provided content is planted.

    ``step_id`` names an executable interaction step and ``content_pointer``
    locates the exact request-template slot that receives the content. The
    compiler requires that slot to hold the canonical ``{{input}}`` binding so
    the reviewed write target cannot drift from the reviewed declaration.
    """

    step_id: str = Field(min_length=1)
    content_pointer: str = Field(min_length=1)


class BrowserStep(AdapterContract):
    kind: Literal["browser"] = "browser"
    role: AdapterStepRole
    source_index: int | None = Field(default=None, ge=0, exclude=True)
    step_id: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    tool: Literal["browser_fill", "browser_click", "browser_press"]
    arguments: dict[str, object]
    repeat: int = Field(default=1, ge=1)
    idempotency_key: str | None = None


class BrowserTextCapture(AdapterContract):
    """Exact rendered-text template used to bind one typed scalar value."""

    template: str = Field(min_length=1, max_length=BROWSER_TEXT_CAPTURE_MAX_LENGTH)
    occurrence: Literal["unique", "first", "last"] = "unique"

    @model_validator(mode="after")
    def validate_template(self) -> Self:
        if self.template.count("{value}") != 1:
            raise ValueError("template must contain exactly one {value} placeholder")
        prefix, suffix = self.template.split("{value}", 1)
        if not prefix and not suffix:
            raise ValueError("template must include literal text around {value}")
        return self


class BrowserRenderedStateObservation(AdapterContract):
    """Reviewed typed state rendered in the browser's attributable page text."""

    name: str = Field(
        min_length=1,
        max_length=BROWSER_STATE_NAME_MAX_LENGTH,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    role: Literal[
        "progress_ordinal",
        "score",
        "budget_remaining",
        "custom",
    ]
    value_type: Literal["integer", "number", "string", "boolean"]
    capture: BrowserTextCapture
    preferred_direction: Literal["increase", "decrease"] | None = None
    success_values: tuple[bool | int | float | str, ...] = Field(
        default=(),
        max_length=BROWSER_STATE_SCALAR_LIMIT,
    )
    failure_values: tuple[bool | int | float | str, ...] = Field(
        default=(),
        max_length=BROWSER_STATE_SCALAR_LIMIT,
    )
    evidence_refs: tuple[str, ...] = Field(
        default=(),
        max_length=BROWSER_STATE_EVIDENCE_LIMIT,
    )
    minimum_value: int | float | None = None
    maximum_value: int | float | None = None
    monotonicity: Literal["nondecreasing", "nonincreasing"] | None = None
    trust_class: Literal["target_reported"] = "target_reported"

    @model_validator(mode="before")
    @classmethod
    def reject_coerced_bounds(cls, value: object) -> object:
        if isinstance(value, Mapping):
            for field in ("minimum_value", "maximum_value"):
                bound = value.get(field)
                if bound is not None and type(bound) not in {int, float}:
                    raise ValueError("numeric bounds must be JSON numbers")
        return value

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if self.preferred_direction is not None and self.value_type not in {
            "integer",
            "number",
        }:
            raise ValueError("preferred_direction requires an integer or number value_type")
        if (self.success_values or self.failure_values) and self.role != "custom":
            raise ValueError("success_values and failure_values require a custom role")
        if set(self.success_values) & set(self.failure_values):
            raise ValueError("success_values and failure_values must not overlap")
        for value in (*self.success_values, *self.failure_values):
            if not self._value_matches_type(value):
                raise ValueError("success_values and failure_values must match value_type")
            if isinstance(value, str) and len(value) > BROWSER_STATE_SCALAR_STRING_MAX_LENGTH:
                raise ValueError("string state values exceed the maximum length")
        if any(
            not value or len(value) > BROWSER_STATE_EVIDENCE_MAX_LENGTH
            for value in self.evidence_refs
        ):
            raise ValueError("evidence_refs must be non-empty bounded strings")
        if self.minimum_value is not None or self.maximum_value is not None:
            if self.value_type not in {"integer", "number"}:
                raise ValueError("numeric bounds require an integer or number value_type")
            if isinstance(self.minimum_value, bool) or isinstance(self.maximum_value, bool):
                raise ValueError("numeric bounds cannot be booleans")
            if any(
                isinstance(value, float) and not math.isfinite(value)
                for value in (self.minimum_value, self.maximum_value)
                if value is not None
            ):
                raise ValueError("numeric bounds must be finite")
        if (
            self.minimum_value is not None
            and self.maximum_value is not None
            and self.minimum_value > self.maximum_value
        ):
            raise ValueError("minimum_value must not exceed maximum_value")
        if self.monotonicity is not None and self.value_type not in {"integer", "number"}:
            raise ValueError("monotonicity requires an integer or number value_type")
        return self

    def _value_matches_type(self, value: object) -> bool:
        if self.value_type == "boolean":
            return type(value) is bool
        if self.value_type == "integer":
            return type(value) is int
        if self.value_type == "number":
            if type(value) is int:
                return True
            return type(value) is float and math.isfinite(value)
        return isinstance(value, str)


class BrowserInteraction(AdapterContract):
    input_locator: object | None = None
    submit_action: object | None = None
    frame_context: object | None = None
    response_witness: str | None = Field(
        default=None,
        min_length=1,
        max_length=BROWSER_TEXT_CAPTURE_MAX_LENGTH,
        exclude_if=lambda value: value is None,
    )
    state_observations: tuple[BrowserRenderedStateObservation, ...] = Field(
        default=(),
        max_length=BROWSER_STATE_OBSERVATION_LIMIT,
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_rendered_state_names(self) -> Self:
        names = [item.name for item in self.state_observations]
        if len(names) != len(set(names)):
            raise ValueError("browser rendered state observation names must be unique")
        if self.response_witness is not None and not self.response_witness.strip():
            raise ValueError("response_witness must contain visible text")
        return self


class ProviderChat(AdapterContract):
    schema_version: Literal["merlin.compiled-target-adapter.v1"] = COMPILED_TARGET_ADAPTER_VERSION
    kind: Literal["provider_chat"] = "provider_chat"
    accepted_interface_aliases: ClassVar[tuple[str, ...]] = (
        "api",
        "open_ai_compatible",
    )
    actor: Literal["target_chat"] = TargetActor.TARGET_CHAT.value
    input_key: Literal["input"] = "input"
    endpoint: str = Field(min_length=1)
    authorized_origins: tuple[str, ...] = Field(min_length=1)
    credential_handle: str | None = None
    target_delivery: TargetDelivery | None = TargetDelivery.DIRECT_USER_CHANNEL
    delivery_resolution: DeliveryResolution = DeliveryResolution.RESOLVED
    cleanup_lifecycle: CleanupLifecycle = CleanupLifecycle.UNKNOWN
    request_constraints: tuple[RequestConstraint, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    state_capabilities: StateCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_capability_steps(self) -> Self:
        _validate_capability_step_ids(
            self.request_constraints,
            self.state_capabilities,
            available_steps={"provider-chat": (AdapterStepRole.INTERACTION, 0)},
        )
        return self


class HttpApi(AdapterContract):
    schema_version: Literal["merlin.compiled-target-adapter.v1"] = COMPILED_TARGET_ADAPTER_VERSION
    kind: Literal["http_api"] = "http_api"
    accepted_interface_aliases: ClassVar[tuple[str, ...]] = ("http_api",)
    actor: Literal["target_request"] = TargetActor.TARGET_REQUEST.value
    input_key: Literal["input"] = "input"
    endpoint: str = Field(min_length=1)
    authorized_origins: tuple[str, ...] = Field(min_length=1)
    credential_handle: str | None = None
    api_transport: ApiTransport = ApiTransport.DIRECT
    target_delivery: TargetDelivery | None = None
    delivery_resolution: DeliveryResolution = DeliveryResolution.UNSPECIFIED
    cleanup_lifecycle: CleanupLifecycle = CleanupLifecycle.UNKNOWN
    steps: tuple[HttpApiStep, ...] = Field(min_length=1)
    content_injection: ContentInjection | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    request_constraints: tuple[RequestConstraint, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    state_capabilities: StateCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_capability_steps(self) -> Self:
        _validate_capability_step_ids(
            self.request_constraints,
            self.state_capabilities,
            available_steps={
                step.step_id: (step.role, index)
                for index, step in enumerate(self.steps)
                if step.step_id is not None
            },
            request_documents={
                step.step_id: step.request_template
                for step in self.steps
                if step.step_id is not None
            },
        )
        if self.content_injection is not None:
            _validate_content_injection(self.content_injection, self.steps)
        return self


class BrowserChat(AdapterContract):
    schema_version: Literal["merlin.compiled-target-adapter.v1"] = COMPILED_TARGET_ADAPTER_VERSION
    kind: Literal["browser_chat"] = "browser_chat"
    accepted_interface_aliases: ClassVar[tuple[str, ...]] = ("browser",)
    actor: Literal["browser_chat_exchange"] = TargetActor.BROWSER_CHAT_EXCHANGE.value
    input_key: Literal["input"] = "input"
    endpoint: str = Field(min_length=1)
    authorized_origins: tuple[str, ...] = Field(min_length=1)
    credential_handle: str | None = None
    target_delivery: TargetDelivery | None = TargetDelivery.DIRECT_USER_CHANNEL
    delivery_resolution: DeliveryResolution = DeliveryResolution.RESOLVED
    cleanup_lifecycle: CleanupLifecycle = CleanupLifecycle.UNKNOWN
    steps: tuple[BrowserStep, ...] = Field(min_length=1)
    interaction: BrowserInteraction
    request_constraints: tuple[RequestConstraint, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    state_capabilities: StateCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_capability_steps(self) -> Self:
        _validate_capability_step_ids(
            self.request_constraints,
            self.state_capabilities,
            available_steps={
                step.step_id: (step.role, index)
                for index, step in enumerate(self.steps)
                if step.step_id is not None
            },
            request_documents={
                step.step_id: step.arguments for step in self.steps if step.step_id is not None
            },
        )
        return self


type CompiledTargetAdapter = Annotated[
    ProviderChat | HttpApi | BrowserChat,
    Field(discriminator="kind"),
]
_COMPILED_ADAPTER = TypeAdapter(CompiledTargetAdapter)
_REQUEST_CONSTRAINTS = TypeAdapter(tuple[RequestConstraint, ...])


class AdapterCompilation(AdapterContract):
    schema_version: Literal["merlin.target-adapter-compilation.v1"] = ADAPTER_COMPILATION_VERSION
    executable: CompiledTargetAdapter
    advisories: tuple[AdapterAdvisory, ...] = ()


def _capability_step_ids(
    constraints: tuple[RequestConstraint, ...], capabilities: StateCapabilities | None
) -> set[str]:
    result = {item.step_id for item in constraints}
    if capabilities is None:
        return result
    if capabilities.identity is not None:
        result.add(capabilities.identity.baseline_selector.step_id)
        if capabilities.identity.result_selector is not None:
            result.add(capabilities.identity.result_selector.step_id)
    for observation in capabilities.observations:
        result.add(observation.baseline_selector.step_id)
        result.add(observation.result_selector.step_id)
    result.update(item.selector.step_id for item in capabilities.enumerations)
    for rule in capabilities.identity_equivalence_rules:
        result.add(rule.left_selector.step_id)
        result.add(rule.right_selector.step_id)
    for ownership in capabilities.resource_ownership:
        result.add(ownership.principal_selector.step_id)
        if ownership.reset_selector is not None:
            result.add(ownership.reset_selector.step_id)
    result.update(item.selector.step_id for item in capabilities.freshness_rules)
    result.update(item.step_id for item in capabilities.affordances)
    if capabilities.checkpoint is not None:
        result.add(capabilities.checkpoint.capture_selector.step_id)
        result.add(capabilities.checkpoint.restore_step_id)
    result.update(item.step_id for item in capabilities.resource_controls)
    result.update(item.step_id for item in capabilities.supported_modalities)
    return result


def _validate_capability_step_ids(
    constraints: tuple[RequestConstraint, ...],
    capabilities: StateCapabilities | None,
    *,
    available_steps: Mapping[str, tuple[AdapterStepRole, int]],
    request_documents: Mapping[str, object] | None = None,
) -> None:
    unknown = _capability_step_ids(constraints, capabilities) - set(available_steps)
    if unknown:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "state_capabilities",
            "capability selectors reference unknown adapter step IDs: "
            + ", ".join(sorted(unknown)),
        )
    if capabilities is None:
        return
    if capabilities.checkpoint is not None:
        checkpoint = capabilities.checkpoint
        capture_role = available_steps[checkpoint.capture_selector.step_id][0]
        restore_role = available_steps[checkpoint.restore_step_id][0]
        required_capture_role = (
            AdapterStepRole.INTERACTION
            if checkpoint.capture_timing == "post_success_response"
            else AdapterStepRole.SETUP
        )
        if capture_role is not required_capture_role:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                "state_capabilities.checkpoint.capture_selector",
                "post-success checkpoint capture requires an interaction response; "
                "a reviewed live handle requires a setup response",
            )
        if restore_role is not AdapterStepRole.SETUP:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                "state_capabilities.checkpoint.restore_step_id",
                "checkpoint restore must reference a setup step",
            )
        restore_documents = request_documents or {}
        if checkpoint.restore_step_id not in restore_documents:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                "state_capabilities.checkpoint.restore_step_id",
                "checkpoint restore step has no reviewable request document",
            )
        try:
            validate_capture_binding(
                restore_documents[checkpoint.restore_step_id],
                checkpoint.restore_request_pointer,
                checkpoint.binding_name,
            )
        except ValueError as exc:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                "state_capabilities.checkpoint.restore_request_pointer",
                str(exc),
            ) from exc
    modality_documents = request_documents or {}
    for modality in capabilities.supported_modalities:
        request_document = modality_documents.get(modality.step_id)
        if (
            available_steps[modality.step_id][0] is not AdapterStepRole.INTERACTION
            or request_document is None
            or not _json_pointer_exists(request_document, modality.request_pointer)
        ):
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                "state_capabilities.supported_modalities",
                "supported modality must reference an existing interaction request value",
            )
    invalid_enumeration_steps = sorted(
        declaration.selector.step_id
        for declaration in capabilities.enumerations
        if available_steps[declaration.selector.step_id][0] is AdapterStepRole.CLEANUP
    )
    if invalid_enumeration_steps:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "state_capabilities.enumerations",
            "finite denominator selectors require setup or interaction responses: "
            + ", ".join(invalid_enumeration_steps),
        )
    setup_only_ids = {item.selector.step_id for item in capabilities.freshness_rules} | {
        item.step_id for item in capabilities.affordances if item.kind.value != "cleanup_state"
    }
    incompatible = sorted(
        step_id
        for step_id in setup_only_ids
        if available_steps.get(step_id, (None, -1))[0] is not AdapterStepRole.SETUP
    )
    if incompatible:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "state_capabilities",
            "freshness rules and state-management affordances require setup steps: "
            + ", ".join(incompatible),
        )
    cleanup_ids = {
        item.step_id for item in capabilities.affordances if item.kind.value == "cleanup_state"
    }
    incompatible_cleanup_ids = sorted(
        step_id
        for step_id in cleanup_ids
        if available_steps.get(step_id, (None, -1))[0] is not AdapterStepRole.CLEANUP
    )
    if incompatible_cleanup_ids:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "state_capabilities.affordances",
            "cleanup_state affordances require cleanup steps: "
            + ", ".join(incompatible_cleanup_ids),
        )
    resource_setup_ids = {
        ownership.principal_selector.step_id for ownership in capabilities.resource_ownership
    } | {
        ownership.reset_selector.step_id
        for ownership in capabilities.resource_ownership
        if ownership.reset_selector is not None
    }
    incompatible_resource_ids = sorted(
        step_id
        for step_id in resource_setup_ids
        if available_steps.get(step_id, (None, -1))[0] is not AdapterStepRole.SETUP
    )
    if incompatible_resource_ids:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "state_capabilities.resource_ownership",
            "resource principal and reset selectors require setup steps: "
            + ", ".join(incompatible_resource_ids),
        )
    paired_selectors = (
        [
            (
                "state_capabilities.identity",
                capabilities.identity.baseline_selector,
                capabilities.identity.result_selector,
            )
        ]
        if capabilities.identity is not None
        else []
    )
    paired_selectors.extend(
        (
            f"state_capabilities.observations[{index}]",
            observation.baseline_selector,
            observation.result_selector,
        )
        for index, observation in enumerate(capabilities.observations)
    )
    paired_selectors.extend(
        (
            f"state_capabilities.identity_equivalence_rules[{index}]",
            rule.left_selector,
            rule.right_selector,
        )
        for index, rule in enumerate(capabilities.identity_equivalence_rules)
    )
    for path, baseline, result in paired_selectors:
        if result is None:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                path,
                f"{path} requires both baseline_selector and result_selector",
            )
        baseline_role, baseline_index = available_steps[baseline.step_id]
        result_role, result_index = available_steps[result.step_id]
        if baseline_role is not AdapterStepRole.SETUP:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                f"{path}.baseline_selector",
                f"{path}.baseline_selector must reference a setup step",
            )
        if result_role is not AdapterStepRole.INTERACTION:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                f"{path}.result_selector",
                f"{path}.result_selector must reference an interaction step",
            )
        if baseline_index >= result_index:
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                path,
                f"{path} baseline must be observed before its interaction result",
            )


_INTERFACE_ALIASES = {
    "api": CompiledAdapterKind.PROVIDER_CHAT,
    "open_ai_compatible": CompiledAdapterKind.PROVIDER_CHAT,
    "http_api": CompiledAdapterKind.HTTP_API,
    "browser": CompiledAdapterKind.BROWSER_CHAT,
}
_INPUT_VALUE_ALIASES = frozenset({"{{input}}", "{{prompt}}", "{{user_input}}"})
_TOP_LEVEL_ADVISORY_KEYS = (
    "api_validation",
    "available_execution_modes",
    "evaluation_uncertainties",
    "known_limitations",
    "language_policy",
    "observed_evidence",
    "observed_surfaces",
    "scenario_identity",
    "session_cardinality",
    "session_constraint",
    "untrusted_evaluation_metadata",
)
_STEP_EVALUATION_KEYS = (
    "cleanup_acknowledgement",
    "expected_status",
    "oracle_predicate",
    "oracle_predicates",
    "response_selector",
    "structured_constraint_rejections",
    "success_witness",
    "terminal_response_contract",
)
_BROWSER_OBSERVATION_KEYS = (
    "authenticated_session_constraints",
    "evidence_refs",
    "input_limit",
    "lifecycle",
    "success_indicator",
)


def _absolute_http_url(value: object, *, path: str, base: str | None = None) -> str:
    candidate = str(value or "").strip()
    if base is not None:
        candidate = urljoin(base.rstrip("/") + "/", candidate)
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_ENDPOINT,
            path,
            f"{path} must be an absolute HTTP(S) URL",
        )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _origin(value: str, *, path: str) -> str:
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise AdapterCompileError(
            AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
            path,
            f"{path} must contain an HTTP(S) origin without a path, query, or fragment",
        )
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _authorized_origins(raw: Mapping[str, object], *, endpoint: str) -> tuple[str, ...]:
    endpoint_origin = _origin(urlunsplit((*urlsplit(endpoint)[:2], "", "", "")), path="endpoint")
    declared = raw.get("authorized_origins", raw.get("allowed_origins"))
    if declared is None:
        return (endpoint_origin,)
    if not isinstance(declared, (list, tuple, set, frozenset)) or not declared:
        raise AdapterCompileError(
            AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
            "authorized_origins",
            "authorized_origins must be a non-empty collection of HTTP(S) origins",
        )
    origins = tuple(
        sorted(
            {
                _origin(
                    _absolute_http_url(item, path=f"authorized_origins[{index}]"),
                    path=f"authorized_origins[{index}]",
                )
                for index, item in enumerate(declared)
            }
        )
    )
    if endpoint_origin not in origins:
        raise AdapterCompileError(
            AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
            "endpoint",
            "adapter endpoint is outside authorized_origins",
        )
    return origins


def _require_authorized_origin(
    endpoint: str, *, authorized_origins: tuple[str, ...], path: str
) -> None:
    origin = _origin(urlunsplit((*urlsplit(endpoint)[:2], "", "", "")), path=path)
    if origin not in authorized_origins:
        raise AdapterCompileError(
            AdapterCompileFailureKind.UNAUTHORIZED_ORIGIN,
            path,
            f"{path} resolves outside authorized_origins",
        )


def _canonical_input_values(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical_input_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_input_values(item) for item in value]
    if isinstance(value, str) and value in _INPUT_VALUE_ALIASES:
        return "{{input}}"
    return value


def _contains_input(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_input(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_input(item) for item in value)
    return value == "{{input}}"


def _capture_dependencies(value: object) -> frozenset[str]:
    if isinstance(value, Mapping):
        return frozenset().union(*(_capture_dependencies(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return frozenset().union(*(_capture_dependencies(item) for item in value))
    if isinstance(value, str) and value.startswith("{{capture:") and value.endswith("}}"):
        return frozenset({value[len("{{capture:") : -2]})
    return frozenset()


def _valid_json_pointer(value: object) -> bool:
    return isinstance(value, str) and (not value or value.startswith("/"))


def _json_pointer_exists(value: object, pointer: str) -> bool:
    if not pointer:
        return True
    current = value
    for raw_part in pointer.removeprefix("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False
    return True


_BROWSER_LOCATOR_ROLES = frozenset({"button", "textbox", "link", "checkbox", "radio", "combobox"})
_BROWSER_LOCATOR_ROLE_PATTERN = "|".join(sorted(_BROWSER_LOCATOR_ROLES))
_CANONICAL_BROWSER_LOCATOR_PATTERN = re.compile(
    rf"^(?P<role>{_BROWSER_LOCATOR_ROLE_PATTERN})\s*\[\s*name\s*=\s*"
    r'(?P<name>"(?:\\.|[^"\\])*")\s*\]$',
    flags=re.IGNORECASE,
)


def _canonical_browser_locator(value: object) -> str | None:
    if isinstance(value, str):
        locator = value.strip()
        if not locator:
            return None
        match = _CANONICAL_BROWSER_LOCATOR_PATTERN.fullmatch(locator)
        if match is None:
            return locator
        try:
            name = json.loads(match.group("name"))
        except json.JSONDecodeError:
            return locator
        if not isinstance(name, str) or not name.strip():
            return locator
        normalized_name = " ".join(name.strip().split())
        return (
            f"{match.group('role').casefold()}"
            f"[name={json.dumps(normalized_name, ensure_ascii=False)}]"
        )
    if not isinstance(value, Mapping) or set(value) != {"role", "name"}:
        return None
    role = value.get("role")
    name = value.get("name")
    if (
        not isinstance(role, str)
        or role.casefold() not in _BROWSER_LOCATOR_ROLES
        or not isinstance(name, str)
        or not name.strip()
    ):
        return None
    normalized_name = " ".join(name.strip().split())
    return f"{role.casefold()}[name={json.dumps(normalized_name, ensure_ascii=False)}]"


_BROWSER_ARGUMENT_KEYS = {
    "browser_fill": frozenset(
        {"locator", "tab_ref", "frame_ref", "element_ref", "value", "step", "timeout_ms"}
    ),
    "browser_click": frozenset(
        {"locator", "tab_ref", "frame_ref", "element_ref", "step", "timeout_ms"}
    ),
    "browser_press": frozenset(
        {
            "locator",
            "tab_ref",
            "frame_ref",
            "element_ref",
            "value",
            "key",
            "step",
            "timeout_ms",
        }
    ),
}


def _browser_arguments(
    tool: str,
    arguments: Mapping[str, object],
    *,
    locator: str,
    path: str,
    advisories: list[AdapterAdvisory],
) -> dict[str, object] | None:
    allowed = _BROWSER_ARGUMENT_KEYS[tool]
    unknown = sorted(str(key) for key in arguments if key not in allowed)
    if unknown:
        _unsupported_step(
            advisories,
            path=path,
            message="Arguments outside the BrowserTool schema were omitted.",
            value=unknown,
        )
    normalized = {
        str(key): _canonical_input_values(value)
        for key, value in arguments.items()
        if key in allowed and key not in {"key", "locator"}
    }
    normalized["locator"] = locator
    for key in ("tab_ref", "frame_ref", "element_ref"):
        if key in normalized and not isinstance(normalized[key], str):
            _unsupported_step(
                advisories,
                path=f"{path}.{key}",
                message=f"Malformed BrowserTool {key} was omitted.",
                value=normalized.pop(key),
            )
    step = normalized.get("step")
    if step is not None and (type(step) is not int or step < 0):
        _unsupported_step(
            advisories,
            path=f"{path}.step",
            message="Malformed BrowserTool step was omitted.",
            value=normalized.pop("step"),
        )
    timeout = normalized.get("timeout_ms")
    if timeout is not None and (type(timeout) is not int or not 100 <= timeout <= 30_000):
        _unsupported_step(
            advisories,
            path=f"{path}.timeout_ms",
            message="Malformed BrowserTool timeout_ms was omitted.",
            value=normalized.pop("timeout_ms"),
        )
    if tool == "browser_press":
        raw_value = arguments.get("value")
        raw_key = arguments.get("key")
        value = raw_value if isinstance(raw_value, str) and raw_value.strip() else raw_key
        if not isinstance(value, str) or not value.strip():
            _unsupported_step(
                advisories,
                path=f"{path}.value",
                message="browser_press requires a non-empty value (or legacy key alias).",
                value=raw_value if raw_value is not None else raw_key,
            )
            return None
        normalized["value"] = value.strip()
    elif "value" in normalized and not isinstance(normalized["value"], str):
        _unsupported_step(
            advisories,
            path=f"{path}.value",
            message="Malformed BrowserTool value was omitted.",
            value=normalized.pop("value"),
        )
    return normalized


def _advisory(kind: AdapterAdvisoryKind, path: str, message: str, value: object) -> AdapterAdvisory:
    return AdapterAdvisory(kind=kind, path=path, message=message, value=value)


def _common_advisories(raw: Mapping[str, object]) -> list[AdapterAdvisory]:
    return [
        _advisory(
            AdapterAdvisoryKind.OBSERVATION,
            key,
            "Advisory reconnaissance metadata is retained outside executable identity.",
            raw[key],
        )
        for key in _TOP_LEVEL_ADVISORY_KEYS
        if key in raw
    ]


def _delivery(
    raw: Mapping[str, object],
    *,
    actor: TargetActor,
    advisories: list[AdapterAdvisory],
) -> tuple[TargetDelivery | None, DeliveryResolution]:
    declared = raw.get("target_delivery")
    if declared is None:
        if actor in {TargetActor.TARGET_CHAT, TargetActor.BROWSER_CHAT_EXCHANGE}:
            return TargetDelivery.DIRECT_USER_CHANNEL, DeliveryResolution.RESOLVED
        return None, DeliveryResolution.UNSPECIFIED
    try:
        delivery = TargetDelivery(str(declared))
    except ValueError:
        advisories.append(
            _advisory(
                AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY,
                "target_delivery",
                "The declared delivery channel is unknown and was not used as execution authority.",
                declared,
            )
        )
        return None, DeliveryResolution.UNRESOLVED
    if actor in {TargetActor.TARGET_CHAT, TargetActor.BROWSER_CHAT_EXCHANGE} and (
        delivery is not TargetDelivery.DIRECT_USER_CHANNEL
    ):
        advisories.append(
            _advisory(
                AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY,
                "target_delivery",
                f"{actor.value} cannot attest the declared delivery channel; execution remains "
                "eligible but the capability requires a different actor.",
                declared,
            )
        )
        return None, DeliveryResolution.UNRESOLVED
    return delivery, DeliveryResolution.RESOLVED


def _step_role(step: Mapping[str, object]) -> AdapterStepRole:
    interaction = step.get("interaction") is True
    cleanup = step.get("cleanup") is True
    if interaction:
        return AdapterStepRole.INTERACTION
    if cleanup:
        return AdapterStepRole.CLEANUP
    return AdapterStepRole.SETUP


def _step_advisories(step: Mapping[str, object], *, path: str) -> list[AdapterAdvisory]:
    advisories = [
        _advisory(
            AdapterAdvisoryKind.EVALUATION_HINT,
            f"{path}.{key}",
            "Evaluation metadata is retained for validator judgment and cannot block execution.",
            step[key],
        )
        for key in _STEP_EVALUATION_KEYS
        if key in step
    ]
    for role_flag in ("interaction", "cleanup"):
        if role_flag in step and not isinstance(step[role_flag], bool):
            advisories.append(
                _advisory(
                    AdapterAdvisoryKind.OBSERVATION,
                    f"{path}.{role_flag}",
                    f"Malformed {role_flag} role metadata was ignored; execution remains "
                    "eligible without that role assertion.",
                    step[role_flag],
                )
            )
    if step.get("interaction") is True and step.get("cleanup") is True:
        advisories.append(
            _advisory(
                AdapterAdvisoryKind.OBSERVATION,
                path,
                "The step declares both interaction and cleanup; it remains executable as an "
                "interaction, while cleanup interpretation requires validator evidence.",
                {"interaction": True, "cleanup": True},
            )
        )
    return advisories


def _unsupported_step(
    advisories: list[AdapterAdvisory], *, path: str, message: str, value: object
) -> None:
    advisories.append(
        _advisory(
            AdapterAdvisoryKind.UNSUPPORTED_CAPABILITY,
            path,
            message,
            value,
        )
    )


def _cleanup_lifecycle(
    raw: Mapping[str, object],
    *,
    roles: tuple[AdapterStepRole, ...],
    advisories: list[AdapterAdvisory],
) -> CleanupLifecycle:
    has_cleanup = AdapterStepRole.CLEANUP in roles
    raw_lifecycle = raw.get("cleanup_lifecycle")
    declared = raw_lifecycle.get("status") if isinstance(raw_lifecycle, Mapping) else None
    normalized = {
        "required": CleanupLifecycle.REQUIRED,
        "not_required": CleanupLifecycle.NOT_REQUIRED,
        "unknown": CleanupLifecycle.UNKNOWN,
        "observed": CleanupLifecycle.UNKNOWN,
        "not_established": CleanupLifecycle.UNKNOWN,
    }.get(declared)
    if has_cleanup:
        if normalized is CleanupLifecycle.NOT_REQUIRED:
            advisories.append(
                _advisory(
                    AdapterAdvisoryKind.OBSERVATION,
                    "cleanup_lifecycle",
                    "An executable cleanup step overrides contradictory not-required metadata.",
                    raw_lifecycle,
                )
            )
        return CleanupLifecycle.REQUIRED
    if normalized is CleanupLifecycle.REQUIRED:
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP,
            "cleanup_lifecycle",
            "cleanup is required but the adapter has no executable cleanup step",
        )
    if raw_lifecycle is not None and normalized is None:
        advisories.append(
            _advisory(
                AdapterAdvisoryKind.OBSERVATION,
                "cleanup_lifecycle",
                "Malformed cleanup metadata remains unknown and cannot establish safe teardown.",
                raw_lifecycle,
            )
        )
    return normalized or CleanupLifecycle.UNKNOWN


def _compile_http_steps(
    raw: Mapping[str, object],
    *,
    base: str,
    authorized_origins: tuple[str, ...],
    advisories: list[AdapterAdvisory],
    include_step_ids: bool = False,
) -> tuple[HttpApiStep, ...]:
    raw_steps = raw.get("api_steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "api_steps",
            "http_api requires at least one executable API step",
        )
    compiled: list[HttpApiStep] = []
    prior_captures: set[str] = set()
    input_binding_failed = False
    seen_step_ids: set[str] = set()
    for index, item in enumerate(raw_steps):
        path = f"api_steps[{index}]"
        if not isinstance(item, Mapping):
            _unsupported_step(
                advisories,
                path=path,
                message="The non-object API step cannot execute and was omitted.",
                value=item,
            )
            continue
        if item.get("interaction") is True and item.get("cleanup") is True:
            raise AdapterCompileError(
                AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP,
                path,
                f"{path} cannot preserve both interaction and cleanup roles",
            )
        step_id = str(item.get("step_id") or "").strip()
        if include_step_ids and (not step_id or step_id in seen_step_ids):
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                f"{path}.step_id",
                "capability-bound API steps require unique non-empty step_id values",
            )
        method = str(item.get("method") or "").strip().upper()
        if not method:
            _unsupported_step(
                advisories,
                path=f"{path}.method",
                message="The API step has no executable method and was omitted.",
                value=item.get("method"),
            )
            continue
        if "request_template" not in item:
            _unsupported_step(
                advisories,
                path=f"{path}.request_template",
                message="The API step has no request template and was omitted.",
                value=None,
            )
            continue
        raw_captures = item.get("captures", {})
        captures = (
            {
                str(key): str(pointer)
                for key, pointer in raw_captures.items()
                if isinstance(key, str) and key.strip() and _valid_json_pointer(pointer)
            }
            if isinstance(raw_captures, Mapping)
            else {}
        )
        if not isinstance(raw_captures, Mapping) or len(captures) != len(raw_captures):
            _unsupported_step(
                advisories,
                path=f"{path}.captures",
                message="Invalid capture pointers were ignored; the request remains executable.",
                value=raw_captures,
            )
        advisories.extend(_step_advisories(item, path=path))
        request_template = _canonical_input_values(item["request_template"])
        try:
            request_bindings = validate_request_bindings(
                request_template, item.get("request_bindings", {})
            )
        except ValueError as error:
            raise AdapterCompileError(
                AdapterCompileFailureKind.NON_EXECUTABLE_INPUT,
                f"{path}.request_bindings",
                str(error),
            ) from error
        missing_captures = _capture_dependencies(request_template) - prior_captures
        if missing_captures:
            _unsupported_step(
                advisories,
                path=f"{path}.request_template",
                message="The API step depends on captures not produced by prior steps and was "
                "omitted.",
                value=sorted(missing_captures),
            )
            continue
        if item.get("interaction") is True and not _contains_input(request_template):
            input_binding_failed = True
            _unsupported_step(
                advisories,
                path=f"{path}.request_template",
                message="The interaction step does not bind the canonical input and was omitted.",
                value=item["request_template"],
            )
            continue
        try:
            endpoint = _absolute_http_url(
                item.get("endpoint", base), path=f"{path}.endpoint", base=base
            )
        except AdapterCompileError:
            _unsupported_step(
                advisories,
                path=f"{path}.endpoint",
                message="The API step endpoint cannot execute and was omitted.",
                value=item.get("endpoint"),
            )
            continue
        try:
            _require_authorized_origin(
                endpoint, authorized_origins=authorized_origins, path=f"{path}.endpoint"
            )
        except AdapterCompileError:
            raise
        repeat = item.get("repeat", 1)
        if type(repeat) is not int or repeat < 1:
            _unsupported_step(
                advisories,
                path=f"{path}.repeat",
                message="Invalid repeat metadata was ignored; one request remains executable.",
                value=repeat,
            )
            repeat = 1
        idempotency_key = item.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            idempotency_key = None
        replay = item.get("replay_after_ambiguous_result") is True
        if replay and idempotency_key is None:
            advisories.append(
                _advisory(
                    AdapterAdvisoryKind.OBSERVATION,
                    f"{path}.replay_after_ambiguous_result",
                    "Replay was disabled because no idempotency key makes it safe.",
                    True,
                )
            )
            replay = False
        compiled.append(
            HttpApiStep(
                role=_step_role(item),
                step_id=step_id if include_step_ids else None,
                endpoint=endpoint,
                method=method,
                request_template=request_template,
                request_bindings=request_bindings,
                captures=captures,
                repeat=repeat,
                idempotency_key=idempotency_key,
                replay_after_ambiguous_result=replay,
                structured_constraint_rejections=item.get("structured_constraint_rejections", ()),
            )
        )
        if include_step_ids:
            seen_step_ids.add(step_id)
        prior_captures.update(captures)
    if not any(step.role is AdapterStepRole.INTERACTION for step in compiled):
        raise AdapterCompileError(
            (
                AdapterCompileFailureKind.NON_EXECUTABLE_INPUT
                if input_binding_failed
                else AdapterCompileFailureKind.NON_EXECUTABLE_STEPS
            ),
            "api_steps",
            (
                "http_api interaction must bind the canonical input"
                if input_binding_failed
                else "http_api requires one interaction step"
            ),
        )
    return tuple(compiled)


def _compile_browser_steps(
    raw: Mapping[str, object],
    *,
    interaction: Mapping[str, object],
    advisories: list[AdapterAdvisory],
    include_step_ids: bool = False,
) -> tuple[tuple[BrowserStep, ...], dict[str, object]]:
    raw_steps = raw.get("browser_steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "browser_steps",
            "browser requires at least one executable browser step",
        )
    compiled: list[BrowserStep] = []
    seen_step_ids: set[str] = set()
    input_locator = _canonical_browser_locator(interaction.get("input_locator"))
    submit_locator = _canonical_browser_locator(interaction.get("submit_action"))
    for index, item in enumerate(raw_steps):
        path = f"browser_steps[{index}]"
        if not isinstance(item, Mapping):
            _unsupported_step(
                advisories,
                path=path,
                message="The non-object browser step cannot execute and was omitted.",
                value=item,
            )
            continue
        raw_source_index = item.get(BROWSER_STEP_SOURCE_INDEX_KEY, index)
        source_index = (
            raw_source_index if type(raw_source_index) is int and raw_source_index >= 0 else None
        )
        if item.get("interaction") is True and item.get("cleanup") is True:
            raise AdapterCompileError(
                AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP,
                path,
                f"{path} cannot preserve both interaction and cleanup roles",
            )
        step_id = str(item.get("step_id") or "").strip()
        if include_step_ids and (not step_id or step_id in seen_step_ids):
            raise AdapterCompileError(
                AdapterCompileFailureKind.CONTRACT_VALIDATION,
                f"{path}.step_id",
                "capability-bound browser steps require unique non-empty step_id values",
            )
        tool = item.get("tool")
        arguments = item.get("arguments")
        if tool not in {"browser_fill", "browser_click", "browser_press"} or not isinstance(
            arguments, Mapping
        ):
            _unsupported_step(
                advisories,
                path=path,
                message="The browser action is not executable by browser_chat_exchange and was "
                "omitted.",
                value=dict(item),
            )
            continue
        step_locator = _canonical_browser_locator(arguments.get("locator"))
        locator = step_locator or (submit_locator if tool == "browser_click" else input_locator)
        if tool == "browser_fill" and locator is None:
            _unsupported_step(
                advisories,
                path=f"{path}.arguments.locator",
                message="The browser fill has no resolvable input locator and was omitted.",
                value=locator,
            )
            continue
        if tool == "browser_click" and locator is None:
            _unsupported_step(
                advisories,
                path=f"{path}.arguments.locator",
                message="The browser submit has no resolvable locator and was omitted.",
                value=locator,
            )
            continue
        if tool == "browser_press" and locator is None:
            _unsupported_step(
                advisories,
                path=f"{path}.arguments.locator",
                message="The browser key submit has no resolvable locator and was omitted.",
                value=locator,
            )
            continue
        normalized_arguments = _browser_arguments(
            str(tool),
            arguments,
            locator=locator,
            path=f"{path}.arguments",
            advisories=advisories,
        )
        if normalized_arguments is None:
            continue
        if tool in {"browser_fill", "browser_press"} and input_locator is None:
            input_locator = locator
        if tool == "browser_click" and submit_locator is None:
            submit_locator = locator
        advisories.extend(_step_advisories(item, path=path))
        role = _step_role(item)
        if tool == "browser_fill" and "interaction" not in item:
            role = AdapterStepRole.INTERACTION
        repeat = item.get("repeat", 1)
        if type(repeat) is not int or repeat < 1:
            _unsupported_step(
                advisories,
                path=f"{path}.repeat",
                message="Invalid repeat metadata was ignored; one action remains executable.",
                value=repeat,
            )
            repeat = 1
        compiled.append(
            BrowserStep(
                role=role,
                source_index=source_index,
                step_id=step_id if include_step_ids else None,
                tool=tool,
                arguments=normalized_arguments,
                repeat=repeat,
                idempotency_key=(
                    item.get("idempotency_key")
                    if isinstance(item.get("idempotency_key"), str)
                    else None
                ),
            )
        )
        if include_step_ids:
            seen_step_ids.add(step_id)
    if not any(step.tool == "browser_fill" for step in compiled):
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "browser_steps",
            "browser chat requires one browser_fill step that can carry the attack input",
        )
    fill_index = next(index for index, step in enumerate(compiled) if step.tool == "browser_fill")
    if not any(
        index > fill_index and step.tool in {"browser_click", "browser_press"}
        for index, step in enumerate(compiled)
    ):
        raise AdapterCompileError(
            AdapterCompileFailureKind.NON_EXECUTABLE_STEPS,
            "browser_steps",
            "browser chat requires a submit action after browser_fill",
        )
    advisories.extend(
        _advisory(
            AdapterAdvisoryKind.OBSERVATION,
            f"interaction_contract.{key}",
            "Browser observation is retained outside executable identity.",
            interaction[key],
        )
        for key in _BROWSER_OBSERVATION_KEYS
        if key in interaction
    )
    normalized_interaction = dict(interaction)
    normalized_interaction["input_locator"] = input_locator
    normalized_interaction["submit_action"] = submit_locator
    return tuple(compiled), normalized_interaction


def _bounded_browser_advisory_value(value: object) -> object:
    """Keep malformed model-authored declarations bounded in durable advisories."""

    try:
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        serialized = repr(value)
    if len(serialized) <= BROWSER_ADVISORY_PREVIEW_MAX_LENGTH:
        return value
    return {
        "truncated": True,
        "original_chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "preview": serialized[:BROWSER_ADVISORY_PREVIEW_MAX_LENGTH],
    }


def _compile_browser_response_witness(
    interaction: Mapping[str, object],
    *,
    advisories: list[AdapterAdvisory],
) -> str | None:
    probe = interaction.get("probe")
    if not isinstance(probe, Mapping) or "response_witness" not in probe:
        return None
    witness = probe.get("response_witness")
    if (
        isinstance(witness, str)
        and witness.strip()
        and len(witness) <= BROWSER_TEXT_CAPTURE_MAX_LENGTH
    ):
        return witness
    _unsupported_step(
        advisories,
        path="interaction_contract.probe.response_witness",
        message=(
            "The response witness is not non-empty exact visible text and was omitted; "
            "browser delivery remains executable with an uncertain response boundary."
        ),
        value=_bounded_browser_advisory_value(witness),
    )
    return None


def _compile_browser_state_observations(
    interaction: Mapping[str, object],
    *,
    advisories: list[AdapterAdvisory],
) -> tuple[BrowserRenderedStateObservation, ...]:
    declared = interaction.get("state_observations")
    if declared is None:
        return ()
    if not isinstance(declared, (list, tuple)):
        _unsupported_step(
            advisories,
            path="interaction_contract.state_observations",
            message=(
                "Rendered state observations must be a list and were omitted; browser "
                "delivery remains executable."
            ),
            value=_bounded_browser_advisory_value(declared),
        )
        return ()
    compiled: list[BrowserRenderedStateObservation] = []
    seen_names: set[str] = set()
    if len(declared) > BROWSER_STATE_OBSERVATION_LIMIT:
        _unsupported_step(
            advisories,
            path="interaction_contract.state_observations",
            message=(
                f"Only the first {BROWSER_STATE_OBSERVATION_LIMIT} rendered state declarations "
                "were retained; browser delivery remains executable."
            ),
            value=len(declared),
        )
    for index, item in enumerate(declared[:BROWSER_STATE_OBSERVATION_LIMIT]):
        path = f"interaction_contract.state_observations[{index}]"
        candidate = dict(item) if isinstance(item, Mapping) else item
        if isinstance(candidate, dict) and candidate.get("role") == "terminal_status":
            _unsupported_step(
                advisories,
                path=f"{path}.role",
                message=(
                    "Rendered page text cannot establish whole-target terminal authority; "
                    "terminal_status was downgraded to advisory custom state."
                ),
                value="terminal_status",
            )
            candidate["role"] = "custom"
            candidate.pop("success_values", None)
            candidate.pop("failure_values", None)
        if isinstance(candidate, dict) and candidate.get("trust_class") not in {
            None,
            "target_reported",
        }:
            _unsupported_step(
                advisories,
                path=f"{path}.trust_class",
                message=(
                    "Rendered target DOM cannot establish host or independent authority; "
                    "trust_class was downgraded to target_reported."
                ),
                value=_bounded_browser_advisory_value(candidate.get("trust_class")),
            )
            candidate["trust_class"] = "target_reported"
        try:
            observation = BrowserRenderedStateObservation.model_validate(candidate)
        except ValidationError:
            _unsupported_step(
                advisories,
                path=path,
                message=(
                    "The rendered state declaration is malformed and was omitted; browser "
                    "delivery remains executable."
                ),
                value=_bounded_browser_advisory_value(item),
            )
            continue
        if observation.name in seen_names:
            _unsupported_step(
                advisories,
                path=f"{path}.name",
                message=(
                    "The rendered state declaration repeats an existing name and was omitted."
                ),
                value=observation.name,
            )
            continue
        seen_names.add(observation.name)
        compiled.append(observation)
    return tuple(compiled)


def _content_injection(raw: Mapping[str, object]) -> ContentInjection | None:
    """Parse the reviewed content-planting declaration."""
    declared = raw.get("content_injection")
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "content_injection",
            "content_injection must be an object when provided",
        )
    try:
        surface = ContentInjection.model_validate(declared)
    except ValidationError as error:
        raise _validation_failure(
            error, prefix="content_injection contract validation failed"
        ) from error
    return surface


def _validate_content_injection(surface: ContentInjection, steps: tuple[HttpApiStep, ...]) -> None:
    by_id = {step.step_id: step for step in steps if step.step_id is not None}
    step = by_id.get(surface.step_id)
    if step is None:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "content_injection.step_id",
            "content_injection.step_id references an unknown adapter step",
        )
    if step.role is not AdapterStepRole.INTERACTION:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "content_injection.step_id",
            "content_injection.step_id must reference an interaction step",
        )
    try:
        validate_input_binding(step.request_template, surface.content_pointer)
    except ValueError as error:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "content_injection.content_pointer",
            str(error),
        ) from error


def _validation_failure(error: ValidationError, *, prefix: str) -> AdapterCompileError:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    rendered: list[str] = []
    for detail in details[:3]:
        location = ".".join(str(part) for part in detail.get("loc", ())) or "adapter"
        message = str(detail.get("msg") or detail.get("type") or "validation failed")
        rendered.append(f"{location}: {message}")
    failure_path = (
        ".".join(str(part) for part in details[0].get("loc", ())) if details else "adapter"
    )
    return AdapterCompileError(
        AdapterCompileFailureKind.CONTRACT_VALIDATION,
        failure_path or "adapter",
        prefix + (f": {'; '.join(rendered)}" if rendered else ""),
    )


def _typed_compile_errors(
    function: Callable[[Mapping[str, object]], AdapterCompilation],
) -> Callable[[Mapping[str, object]], AdapterCompilation]:
    @wraps(function)
    def wrapped(value: Mapping[str, object]) -> AdapterCompilation:
        try:
            return function(value)
        except AdapterCompileError:
            raise
        except ValidationError as error:
            raise _validation_failure(
                error, prefix="compiled adapter contract validation failed"
            ) from error

    return wrapped


@_typed_compile_errors
def compile_target_adapter(value: Mapping[str, object]) -> AdapterCompilation:
    """Compile a legacy mapping without letting advisory metadata veto execution."""
    if not isinstance(value, Mapping):
        raise AdapterCompileError(
            AdapterCompileFailureKind.MALFORMED_CONTRACT,
            "adapter",
            "adapter must be an object",
        )
    raw = {str(key): item for key, item in value.items()}
    source_interface = str(raw.get("interface") or "").strip().casefold()
    kind = _INTERFACE_ALIASES.get(source_interface)
    if kind is None:
        raise AdapterCompileError(
            AdapterCompileFailureKind.UNKNOWN_INTERFACE,
            "interface",
            "adapter interface must be api, open_ai_compatible, http_api, or browser",
        )
    if raw.get("content_injection") is not None and kind is not CompiledAdapterKind.HTTP_API:
        raise AdapterCompileError(
            AdapterCompileFailureKind.CONTRACT_VALIDATION,
            "content_injection",
            "content_injection is supported only for http_api interfaces",
        )
    endpoint = _absolute_http_url(raw.get("endpoint"), path="endpoint")
    authorized_origins = _authorized_origins(raw, endpoint=endpoint)
    credential = raw.get("credential_handle")
    if credential is not None and (not isinstance(credential, str) or not credential.strip()):
        raise AdapterCompileError(
            AdapterCompileFailureKind.MALFORMED_CREDENTIAL,
            "credential_handle",
            "credential_handle must be a non-empty string when provided",
        )
    credential_handle = credential.strip() if isinstance(credential, str) else None
    advisories = _common_advisories(raw)
    request_constraints = _REQUEST_CONSTRAINTS.validate_python(raw.get("request_constraints") or ())
    raw_state_capabilities = raw.get("state_capabilities")
    state_capabilities = (
        StateCapabilities.model_validate(raw_state_capabilities)
        if raw_state_capabilities is not None
        else None
    )
    include_step_ids = bool(
        request_constraints
        or state_capabilities is not None
        or raw.get("content_injection") is not None
    )

    if kind is CompiledAdapterKind.PROVIDER_CHAT:
        _validate_capability_step_ids(
            request_constraints,
            state_capabilities,
            available_steps={"provider-chat": (AdapterStepRole.INTERACTION, 0)},
        )
        delivery, delivery_resolution = _delivery(
            raw, actor=TargetActor.TARGET_CHAT, advisories=advisories
        )
        raw_steps = raw.get("api_steps")
        if isinstance(raw_steps, (list, tuple)) and any(
            isinstance(step, Mapping) and step.get("cleanup") is True for step in raw_steps
        ):
            raise AdapterCompileError(
                AdapterCompileFailureKind.NON_EXECUTABLE_CLEANUP,
                "api_steps",
                "target_chat cannot execute the adapter's required API cleanup step",
            )
        cleanup_lifecycle = _cleanup_lifecycle(raw, roles=(), advisories=advisories)
        executable: CompiledTargetAdapter = ProviderChat(
            endpoint=endpoint,
            authorized_origins=authorized_origins,
            credential_handle=credential_handle,
            target_delivery=delivery,
            delivery_resolution=delivery_resolution,
            cleanup_lifecycle=cleanup_lifecycle,
            request_constraints=request_constraints,
            state_capabilities=state_capabilities,
        )
    elif kind is CompiledAdapterKind.HTTP_API:
        delivery, delivery_resolution = _delivery(
            raw, actor=TargetActor.TARGET_REQUEST, advisories=advisories
        )
        try:
            transport = ApiTransport(str(raw.get("api_transport") or ApiTransport.DIRECT))
        except ValueError as error:
            raise AdapterCompileError(
                AdapterCompileFailureKind.NON_EXECUTABLE_TRANSPORT,
                "api_transport",
                "api_transport must be direct or browser_context",
            ) from error
        steps = _compile_http_steps(
            raw,
            base=endpoint,
            authorized_origins=authorized_origins,
            advisories=advisories,
            include_step_ids=include_step_ids,
        )
        content_injection = _content_injection(raw)
        _validate_capability_step_ids(
            request_constraints,
            state_capabilities,
            available_steps={
                step.step_id: (step.role, index)
                for index, step in enumerate(steps)
                if step.step_id is not None
            },
            request_documents={
                step.step_id: step.request_template for step in steps if step.step_id is not None
            },
        )
        cleanup_lifecycle = _cleanup_lifecycle(
            raw,
            roles=tuple(step.role for step in steps),
            advisories=advisories,
        )
        executable = HttpApi(
            endpoint=endpoint,
            authorized_origins=authorized_origins,
            credential_handle=credential_handle,
            api_transport=transport,
            target_delivery=delivery,
            delivery_resolution=delivery_resolution,
            cleanup_lifecycle=cleanup_lifecycle,
            steps=steps,
            content_injection=content_injection,
            request_constraints=request_constraints,
            state_capabilities=state_capabilities,
        )
    else:
        delivery, delivery_resolution = _delivery(
            raw, actor=TargetActor.BROWSER_CHAT_EXCHANGE, advisories=advisories
        )
        contract = raw.get("interaction_contract")
        contract = contract if isinstance(contract, Mapping) else {}
        steps, contract = _compile_browser_steps(
            raw,
            interaction=contract,
            advisories=advisories,
            include_step_ids=include_step_ids,
        )
        _validate_capability_step_ids(
            request_constraints,
            state_capabilities,
            available_steps={
                step.step_id: (step.role, index)
                for index, step in enumerate(steps)
                if step.step_id is not None
            },
            request_documents={
                step.step_id: step.arguments for step in steps if step.step_id is not None
            },
        )
        cleanup_lifecycle = _cleanup_lifecycle(
            raw,
            roles=tuple(step.role for step in steps),
            advisories=advisories,
        )
        executable = BrowserChat(
            endpoint=endpoint,
            authorized_origins=authorized_origins,
            credential_handle=credential_handle,
            target_delivery=delivery,
            delivery_resolution=delivery_resolution,
            cleanup_lifecycle=cleanup_lifecycle,
            steps=steps,
            interaction=BrowserInteraction(
                input_locator=contract.get("input_locator"),
                submit_action=contract.get("submit_action"),
                frame_context=contract.get("frame_context"),
                response_witness=_compile_browser_response_witness(
                    contract,
                    advisories=advisories,
                ),
                state_observations=_compile_browser_state_observations(
                    contract,
                    advisories=advisories,
                ),
            ),
            request_constraints=request_constraints,
            state_capabilities=state_capabilities,
        )
    return AdapterCompilation(executable=executable, advisories=tuple(advisories))


def read_target_adapter(value: object) -> AdapterCompilation:
    """Read a compiled envelope or compile a persisted v1/raw adapter mapping."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise AdapterCompileError(
                AdapterCompileFailureKind.MALFORMED_CONTRACT,
                "adapter",
                "adapter JSON is malformed",
            ) from error
    if isinstance(value, AdapterCompilation):
        return value
    if not isinstance(value, Mapping):
        raise AdapterCompileError(
            AdapterCompileFailureKind.MALFORMED_CONTRACT,
            "adapter",
            "adapter must be an object or JSON object",
        )
    try:
        if value.get("schema_version") == ADAPTER_COMPILATION_VERSION:
            return AdapterCompilation.model_validate(value)
        if value.get("schema_version") == COMPILED_TARGET_ADAPTER_VERSION:
            return AdapterCompilation(executable=_COMPILED_ADAPTER.validate_python(value))
    except ValidationError as error:
        raise _validation_failure(
            error, prefix="persisted compiled adapter contract validation failed"
        ) from error
    return compile_target_adapter(value)


def compiled_target_adapter_digest(value: AdapterCompilation | CompiledTargetAdapter) -> str:
    """Hash only normalized executable semantics, never advisory interpretation."""
    executable = value.executable if isinstance(value, AdapterCompilation) else value
    payload = executable.model_dump(mode="json")
    constraints = executable.request_constraints
    capabilities = executable.state_capabilities
    payload.pop("request_constraints", None)
    payload.pop("state_capabilities", None)
    if constraints:
        payload["request_constraints"] = request_constraints_semantic_projection(constraints)
    if capabilities is not None:
        state_projection = state_capabilities_semantic_projection(capabilities)
        if state_projection:
            payload["state_capabilities"] = state_projection
    # Optional bindings must not invalidate already-reviewed legacy adapters.
    if isinstance(executable, HttpApi):
        for step in payload["steps"]:
            if step.get("step_id") is None:
                step.pop("step_id", None)
            if not step.get("request_bindings"):
                step.pop("request_bindings", None)
    elif isinstance(executable, BrowserChat):
        for step in payload["steps"]:
            if step.get("step_id") is None:
                step.pop("step_id", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
