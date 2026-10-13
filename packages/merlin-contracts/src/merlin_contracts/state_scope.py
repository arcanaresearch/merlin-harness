"""Versioned durable envelope for one target action and its observed state."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .target_capabilities import STATE_SELECTOR_SCHEMA_VERSION

STATE_SCOPE_SCHEMA_VERSION = "merlin.state-scope.v1"
STATE_SELECTOR_CONTRACT_VERSION = STATE_SELECTOR_SCHEMA_VERSION
LEGACY_ATTEMPT_SCHEMA_VERSION = "merlin.technique-attempt.v1"


class StateScopeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateScopeActionKind(StrEnum):
    RECONNAISSANCE = "reconnaissance"
    BASELINE = "baseline"
    CANDIDATE = "candidate"
    VALIDATION = "validation"
    CLEANUP = "cleanup"


class EvidenceAvailability(StrEnum):
    PRESENT = "present"
    EMPTY = "empty"
    MISSING = "missing"


class MissingEvidenceReason(StrEnum):
    NOT_YET_OBSERVED = "not_yet_observed"
    NOT_APPLICABLE = "not_applicable"
    SELECTOR_UNAVAILABLE = "selector_unavailable"
    TARGET_OMITTED = "target_omitted"
    TRANSPORT_FAILED = "transport_failed"
    DEADLINE_EXPIRED = "deadline_expired"
    PARSE_FAILED = "parse_failed"
    CAPTURE_INCOMPLETE = "capture_incomplete"


class MissingEvidence(StateScopeContract):
    reason: MissingEvidenceReason
    retryable: bool = False
    code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


class StateScopeEvidence(StateScopeContract):
    """A required envelope slot; absence must be represented explicitly."""

    availability: EvidenceAvailability
    evidence_refs: tuple[str, ...] = ()
    item_count: int = Field(default=0, ge=0)
    missing: MissingEvidence | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")
        if self.availability is EvidenceAvailability.MISSING:
            if self.missing is None:
                raise ValueError("missing evidence requires a typed missing reason")
            if self.item_count:
                raise ValueError("missing evidence cannot report captured items")
            return self
        if self.missing is not None:
            raise ValueError("present or empty evidence cannot carry a missing reason")
        if self.availability is EvidenceAvailability.PRESENT:
            if not self.evidence_refs or self.item_count < 1:
                raise ValueError("present evidence requires references and a positive item_count")
            if self.item_count != len(self.evidence_refs):
                raise ValueError("item_count must match the captured evidence references")
        elif self.evidence_refs or self.item_count:
            raise ValueError("empty evidence cannot contain references or items")
        return self


class KeyedIdentityDigest(StateScopeContract):
    """A non-reversible identity comparison value safe for durable storage."""

    kind: Literal["keyed_digest"] = "keyed_digest"
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpaqueIdentityReference(StateScopeContract):
    """An internal handle; it is never a target-issued identifier or credential value."""

    kind: Literal["opaque_reference"] = "opaque_reference"
    namespace: Literal["credential", "identity", "state"]
    reference_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


type SecretSafeIdentity = Annotated[
    KeyedIdentityDigest | OpaqueIdentityReference,
    Field(discriminator="kind"),
]


class StateScopeEnvelope(StateScopeContract):
    """The durable, single-action boundary used by new-format campaigns."""

    schema_version: Literal["merlin.state-scope.v1"] = STATE_SCOPE_SCHEMA_VERSION
    selector_contract_version: Literal["merlin.state-selector.v1"] = STATE_SELECTOR_CONTRACT_VERSION
    revision: int = Field(default=1, ge=1)
    scope_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    action_kind: StateScopeActionKind
    target_authority: str = Field(min_length=1)
    candidate_id: str | None = Field(default=None, min_length=1)
    isolation_lease_id: str | None = Field(default=None, min_length=1)
    principal_identity: SecretSafeIdentity | None = None
    physical_state_identity: SecretSafeIdentity | None = None
    before: StateScopeEvidence
    action: StateScopeEvidence
    responses: StateScopeEvidence
    after: StateScopeEvidence

    @model_validator(mode="after")
    def validate_candidate_binding(self) -> Self:
        if self.action_kind is StateScopeActionKind.CANDIDATE and self.candidate_id is None:
            raise ValueError("candidate actions require candidate_id")
        return self


class StateScopeMigrationContract(StateScopeContract):
    """Reader/writer compatibility during the state-scope migration."""

    readable_versions: tuple[
        Literal["merlin.technique-attempt.v1", "merlin.state-scope.v1"], ...
    ] = (LEGACY_ATTEMPT_SCHEMA_VERSION, STATE_SCOPE_SCHEMA_VERSION)
    writable_version: Literal["merlin.state-scope.v1"] = STATE_SCOPE_SCHEMA_VERSION
    preserve_legacy_digests: Literal[True] = True
    dual_execution: Literal[False] = False
    allow_new_format_downgrade: Literal[False] = False

    @model_validator(mode="after")
    def validate_read_versions(self) -> Self:
        if len(self.readable_versions) != 2 or set(self.readable_versions) != {
            LEGACY_ATTEMPT_SCHEMA_VERSION,
            STATE_SCOPE_SCHEMA_VERSION,
        }:
            raise ValueError("migration readers must support legacy and state-scope versions")
        return self


class StateScopeRollbackContract(StateScopeContract):
    """Fixed-forward rollback behavior for a release that cannot write v1 scopes."""

    stop_new_format_claims: Literal[True] = True
    settle_acknowledged_actions_once: Literal[True] = True
    resume_legacy_campaigns: Literal[True] = True
    pause_new_format_campaigns: Literal[True] = True
    keep_envelopes_readable: Literal[True] = True
    keep_envelopes_immutable: Literal[True] = True
    downgrade_new_format_campaigns: Literal[False] = False


STATE_SCOPE_MIGRATION = StateScopeMigrationContract()
STATE_SCOPE_ROLLBACK = StateScopeRollbackContract()
