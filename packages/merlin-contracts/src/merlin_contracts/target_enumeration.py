"""Canonical target-authored finite enumeration receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .target_capabilities import (
    StateEnumerationCompleteness,
    StateEnumerationDeclaration,
    StateEnumerationShape,
)

TARGET_STATE_ENUMERATION_SCHEMA_VERSION = "merlin.target-state-enumeration.v1"


class StateEnumerationReceipt(BaseModel):
    """Validated target-authoritative enumeration bound to a reviewed selector."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    schema_id: Literal["merlin.target-state-enumeration.v1"] = Field(
        default=TARGET_STATE_ENUMERATION_SCHEMA_VERSION,
        alias="schema",
    )
    authority: Literal["target"] = "target"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    scope_dimension: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    members_by_scope: dict[str, tuple[str, ...]]
    complete: bool
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    enumeration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_members(self) -> StateEnumerationReceipt:
        if not self.members_by_scope:
            raise ValueError("enumeration receipts require at least one scope entry")
        if self.scope_dimension is None and set(self.members_by_scope) != {"*"}:
            raise ValueError("flat enumerations require the '*' scope")
        if self.scope_dimension is not None and "*" in self.members_by_scope:
            if len(self.members_by_scope) != 1 or self.members_by_scope["*"]:
                raise ValueError(
                    "scoped '*' is reserved for an authoritative empty denominator"
                )
        for scope, members in self.members_by_scope.items():
            if not scope:
                raise ValueError("enumeration scope values cannot be empty")
            if any(not member for member in members):
                raise ValueError("enumeration member ids cannot be empty")
            if len(set(members)) != len(members):
                raise ValueError("enumeration member ids must be unique within each scope")
        expected_digest = _digest_json(
            {
                "name": self.name,
                "scope_dimension": self.scope_dimension,
                "members_by_scope": {
                    key: list(value) for key, value in sorted(self.members_by_scope.items())
                },
                "complete": self.complete,
                "binding_digest": self.binding_digest,
            }
        )
        if self.enumeration_digest != expected_digest:
            raise ValueError("enumeration digest does not match canonical content")
        return self


def build_state_enumeration(
    declaration: StateEnumerationDeclaration,
    *,
    selected_value: object,
    binding_context: Mapping[str, object],
) -> StateEnumerationReceipt:
    """Validate selected target data and build a canonical enumeration receipt."""

    if declaration.shape is StateEnumerationShape.FLAT:
        members_by_scope = {"*": _member_ids(selected_value, context=declaration.name)}
    else:
        if not isinstance(selected_value, Mapping):
            raise ValueError("scoped enumeration selectors must yield an object")
        members_by_scope = {
            _scope_value(scope): _member_ids(
                members,
                context=f"{declaration.name}:{scope}",
            )
            for scope, members in selected_value.items()
        }
        if not members_by_scope:
            members_by_scope = {"*": ()}

    selector = declaration.selector
    binding_digest = _digest_json(
        {
            **dict(binding_context),
            "name": declaration.name,
            "step_id": selector.step_id,
            "evaluation_root": selector.evaluation_root,
            "pointer": selector.pointer,
            "shape": declaration.shape.value,
            "scope_dimension": declaration.scope_dimension,
            "completeness": declaration.completeness.value,
        }
    )
    complete = declaration.completeness is StateEnumerationCompleteness.COMPLETE
    evidence_refs = tuple(
        dict.fromkeys((*declaration.evidence_refs, *selector.evidence_refs))
    )
    if not evidence_refs:
        raise ValueError("state enumerations require reviewed evidence references")
    content = {
        "name": declaration.name,
        "scope_dimension": declaration.scope_dimension,
        "members_by_scope": {
            key: list(value) for key, value in sorted(members_by_scope.items())
        },
        "complete": complete,
        "binding_digest": binding_digest,
    }
    return StateEnumerationReceipt(
        name=declaration.name,
        scope_dimension=declaration.scope_dimension,
        members_by_scope=members_by_scope,
        complete=complete,
        evidence_refs=evidence_refs,
        binding_digest=binding_digest,
        enumeration_digest=_digest_json(content),
    )


def complete_enumeration_members(
    receipt: StateEnumerationReceipt | Mapping[str, object],
) -> dict[str, tuple[str, ...]] | None:
    """Return a copy of the finite denominator only for a valid complete receipt."""

    validated = (
        receipt
        if isinstance(receipt, StateEnumerationReceipt)
        else StateEnumerationReceipt.model_validate(receipt)
    )
    if not validated.complete:
        return None
    return {scope: tuple(members) for scope, members in validated.members_by_scope.items()}


def _member_ids(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"enumeration {context} must yield an array of member ids")
    members = tuple(value)
    if any(not isinstance(member, str) or not member for member in members):
        raise ValueError(f"enumeration {context} member ids must be non-empty strings")
    if len(set(members)) != len(members):
        raise ValueError(f"enumeration {context} member ids must be unique")
    return members


def _scope_value(value: object) -> str:
    if not isinstance(value, str) or not value or value == "*":
        raise ValueError("scoped enumeration keys must be non-empty strings other than '*'")
    return value


def _digest_json(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()
