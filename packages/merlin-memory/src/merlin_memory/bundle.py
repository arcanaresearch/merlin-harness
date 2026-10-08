"""Versioned, canonical portable-memory bundle domain."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUNDLE_SCHEMA = "merlin.portable-memory/v1"
MAX_RECORDS = 256
SECRET_TERMS = frozenset(
    {
        "api_key",
        "apikey",
        "authority",
        "authorization",
        "credential",
        "grant",
        "password",
        "secret",
        "token",
    }
)


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BundleProvenance(MemoryModel):
    source: str = Field(min_length=1, max_length=512)
    exported_by: str = Field(min_length=1, max_length=256)
    reviewed_by: tuple[str, ...] = Field(min_length=1, max_length=32)
    created_at: datetime


class Applicability(MemoryModel):
    repository_ids: tuple[str, ...] = Field(default=(), max_length=64)
    target_ids: tuple[str, ...] = Field(default=(), max_length=64)
    provider_ids: tuple[str, ...] = Field(default=(), max_length=64)
    operator_ids: tuple[str, ...] = Field(default=(), max_length=64)
    target_kinds: tuple[str, ...] = Field(default=(), max_length=32)
    tags: tuple[str, ...] = Field(default=(), max_length=64)
    scope_ids: tuple[str, ...] = Field(default=(), max_length=64)


class ReviewedSkill(MemoryModel):
    kind: Literal["reviewed_skill"] = "reviewed_skill"
    name: str = Field(min_length=1, max_length=128)
    instructions: str = Field(min_length=1, max_length=65_536)


class GuidanceMemory(MemoryModel):
    kind: Literal["guidance"] = "guidance"
    guidance_id: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=65_536)


class FindingReference(MemoryModel):
    kind: Literal["finding_reference"] = "finding_reference"
    finding_id: str = Field(min_length=1, max_length=256)
    evidence_references: tuple[str, ...] = Field(min_length=1, max_length=64)


class SealedLesson(MemoryModel):
    kind: Literal["sealed_lesson"] = "sealed_lesson"
    statement: str = Field(min_length=1, max_length=16_384)
    evidence_references: tuple[str, ...] = Field(min_length=1, max_length=64)


class CapabilityObservation(MemoryModel):
    kind: Literal["capability_observation"] = "capability_observation"
    capability: str = Field(min_length=1, max_length=256)
    observation: str = Field(min_length=1, max_length=16_384)


class NonSecretConfig(MemoryModel):
    kind: Literal["non_secret_config"] = "non_secret_config"
    values: dict[str, Any]

    @model_validator(mode="after")
    def reject_secrets(self) -> NonSecretConfig:
        _reject_secret_values(self.values)
        return self


MemoryPayload = Annotated[
    ReviewedSkill
    | GuidanceMemory
    | FindingReference
    | SealedLesson
    | CapabilityObservation
    | NonSecretConfig,
    Field(discriminator="kind"),
]


class MemoryRecord(MemoryModel):
    id: str = Field(min_length=1, max_length=256)
    payload: MemoryPayload
    provenance: BundleProvenance
    applicability: Applicability = Applicability()
    redactions: tuple[str, ...] = Field(default=(), max_length=64)
    expires_at: datetime | None = None
    digest: str = ""

    @model_validator(mode="after")
    def verify_review_and_digest(self) -> MemoryRecord:
        if not self.provenance.reviewed_by:
            raise ValueError("portable records must be reviewed")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        expected = _digest(self.model_dump(mode="json", exclude={"digest"}))
        if self.digest and self.digest != expected:
            raise ValueError("record digest mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        return self


class MemoryBundle(MemoryModel):
    schema_version: Literal["merlin.portable-memory/v1"] = Field(
        default=BUNDLE_SCHEMA, alias="schema"
    )
    bundle_id: str = Field(min_length=1, max_length=256)
    provenance: BundleProvenance
    records: tuple[MemoryRecord, ...] = Field(min_length=1, max_length=MAX_RECORDS)
    digest: str = ""

    @model_validator(mode="after")
    def verify_bundle(self) -> MemoryBundle:
        ids = [record.id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("bundle contains duplicate record ids")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"digest"}))
        if self.digest and self.digest != expected:
            raise ValueError("bundle digest mismatch")
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        return self


def build_bundle(
    bundle_id: str,
    provenance: BundleProvenance,
    records: tuple[MemoryRecord, ...],
) -> MemoryBundle:
    return MemoryBundle(bundle_id=bundle_id, provenance=provenance, records=records)


def encode_bundle(bundle: MemoryBundle) -> bytes:
    return _canonical(bundle.model_dump(mode="json", by_alias=True))


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _reject_secret_values(value: object, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(term in normalized for term in SECRET_TERMS):
                raise ValueError(f"secret-like configuration key is not portable: {path}.{key}")
            _reject_secret_values(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_values(item, f"{path}[{index}]")
