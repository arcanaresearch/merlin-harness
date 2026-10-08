"""Attested language-effect evidence for model-mediated prompt transforms."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

_LANGUAGE = re.compile(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", re.IGNORECASE)
_TRANSFORMS = frozenset({"native", "translated", "mixed", "script-switching", "register"})
ExecutionClass = Literal["locally_exact", "model_mediated_verified", "unavailable"]
VerificationStatus = Literal["verified", "unverifiable", "mismatch"]


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LanguageReachability:
    transform_id: str
    production_reachable: bool
    locally_executable: bool
    execution_path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class LanguageExecutionEvidence:
    schema: str
    requested_transform_id: str
    applied_transform_id: str | None
    requested_language: str
    actual_language: str
    source_digest: str
    output_digest: str
    executor_id: str
    language_match: bool
    receipt_digest: str
    execution_class: ExecutionClass = "model_mediated_verified"
    verification_status: VerificationStatus = "verified"
    transform_credit_eligible: bool = True

    def __post_init__(self) -> None:
        for language in (self.requested_language, self.actual_language):
            if _LANGUAGE.fullmatch(language) is None:
                raise ValueError("language evidence requires canonical BCP-47-like tokens")
        if not self.requested_transform_id.strip():
            raise ValueError("requested language transform must be non-empty")
        legacy_unsigned = {
            "schema": self.schema,
            "requested_transform_id": self.requested_transform_id,
            "applied_transform_id": self.applied_transform_id,
            "requested_language": self.requested_language,
            "actual_language": self.actual_language,
            "source_digest": self.source_digest,
            "output_digest": self.output_digest,
            "executor_id": self.executor_id,
            "language_match": self.language_match,
        }
        unsigned = {
            **legacy_unsigned,
            "execution_class": self.execution_class,
            "verification_status": self.verification_status,
            "transform_credit_eligible": self.transform_credit_eligible,
        }
        if self.schema != "merlin.language-execution-evidence.v1" or self.receipt_digest not in {
            _digest(legacy_unsigned),
            _digest(unsigned),
        }:
            raise ValueError("language execution evidence digest mismatch")
        if self.language_match != (self.requested_language == self.actual_language):
            raise ValueError("language match flag differs from requested/actual language")
        expected_status: VerificationStatus = (
            "unverifiable"
            if self.applied_transform_id is None
            else "verified"
            if self.applied_transform_id == self.requested_transform_id
            else "mismatch"
        )
        if self.verification_status != expected_status:
            raise ValueError("language transform verification status differs from receipt")
        if self.transform_credit_eligible != (expected_status == "verified"):
            raise ValueError("transform-specific credit requires a verified applied transform")
        expected_class: ExecutionClass = (
            "locally_exact"
            if expected_status == "verified"
            and self.executor_id == "merlin.parseltongue.deterministic.v1"
            else "model_mediated_verified"
            if expected_status == "verified"
            else "unavailable"
        )
        if self.execution_class != expected_class:
            raise ValueError("language execution class differs from verification status")


@dataclass(frozen=True, slots=True)
class ModelMediatedExecutorContract:
    schema: str
    executor_id: str
    execution_class: str
    requested_transform_id: str
    requested_language: str
    requested_instruction: str
    instruction_digest: str
    required_capabilities: tuple[str, ...]
    contract_digest: str

    def __post_init__(self) -> None:
        unsigned = {
            "schema": self.schema,
            "executor_id": self.executor_id,
            "execution_class": self.execution_class,
            "requested_transform_id": self.requested_transform_id,
            "requested_language": self.requested_language,
            "requested_instruction": self.requested_instruction,
            "instruction_digest": self.instruction_digest,
            "required_capabilities": self.required_capabilities,
        }
        if (
            self.schema != "merlin.model-mediated-executor.v1"
            or self.execution_class != "model_mediated_verified"
            or not self.executor_id.strip()
            or not self.requested_transform_id.strip()
            or _LANGUAGE.fullmatch(self.requested_language) is None
            or self.instruction_digest != _digest(self.requested_instruction)
            or self.contract_digest != _digest(unsigned)
        ):
            raise ValueError("invalid reviewed model-mediated executor contract")


def reviewed_model_executor_contract(
    instruction: str,
    *,
    requested_transform_id: str,
    requested_language: str,
    required_capabilities: tuple[str, ...] = (),
) -> ModelMediatedExecutorContract:
    """Seal the exact instruction supplied to the reviewed attacker-model executor."""
    requested_language = requested_language.strip().casefold() or "und"
    unsigned = {
        "schema": "merlin.model-mediated-executor.v1",
        "executor_id": "merlin.attacker-model.reviewed-instruction.v1",
        "execution_class": "model_mediated_verified",
        "requested_transform_id": requested_transform_id,
        "requested_language": requested_language,
        "requested_instruction": instruction,
        "instruction_digest": _digest(instruction),
        "required_capabilities": required_capabilities,
    }
    return ModelMediatedExecutorContract(**unsigned, contract_digest=_digest(unsigned))


def language_reachability(transform_id: str) -> LanguageReachability:
    """Describe the real envelope path without claiming a local translator exists."""
    supported = transform_id in _TRANSFORMS
    return LanguageReachability(
        transform_id=transform_id,
        production_reachable=supported,
        locally_executable=False,
        execution_path="merlin_agents.transforms.materialize_transform_envelope"
        if supported
        else None,
        reason=(
            "production reaches a model-mediated assignment envelope; actual language "
            "requires attestation"
            if supported
            else "no reviewed envelope or executor exists"
        ),
    )


def record_language_execution(
    source: str,
    output: str,
    *,
    requested_transform_id: str,
    applied_transform_id: str | None,
    requested_language: str,
    actual_language: str,
    executor_id: str,
    source_digest: str | None = None,
    output_digest: str | None = None,
) -> LanguageExecutionEvidence:
    """Seal provider/verifier observations; never infer actual language from the request."""
    requested_language = requested_language.strip().casefold()
    actual_language = actual_language.strip().casefold()
    if not executor_id.strip():
        raise ValueError("language execution evidence requires an executor identity")
    verification_status: VerificationStatus = (
        "unverifiable"
        if applied_transform_id is None
        else "verified"
        if applied_transform_id == requested_transform_id
        else "mismatch"
    )
    execution_class: ExecutionClass = (
        "locally_exact"
        if verification_status == "verified"
        and executor_id == "merlin.parseltongue.deterministic.v1"
        else "model_mediated_verified"
        if verification_status == "verified"
        else "unavailable"
    )
    unsigned = {
        "schema": "merlin.language-execution-evidence.v1",
        "requested_transform_id": requested_transform_id,
        "applied_transform_id": applied_transform_id,
        "requested_language": requested_language,
        "actual_language": actual_language,
        "source_digest": source_digest or _digest(source),
        "output_digest": output_digest or _digest(output),
        "executor_id": executor_id,
        "language_match": requested_language == actual_language,
        "execution_class": execution_class,
        "verification_status": verification_status,
        "transform_credit_eligible": verification_status == "verified",
    }
    return LanguageExecutionEvidence(**unsigned, receipt_digest=_digest(unsigned))
