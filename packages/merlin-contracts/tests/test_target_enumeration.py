import pytest
from merlin_contracts.target_capabilities import (
    StateCapabilities,
    StateEnumerationDeclaration,
    state_capabilities_semantic_projection,
)
from merlin_contracts.target_enumeration import (
    StateEnumerationReceipt,
    build_state_enumeration,
    complete_enumeration_members,
)
from pydantic import ValidationError


def _declaration(
    *,
    shape: str = "flat",
    completeness: str = "complete",
) -> StateEnumerationDeclaration:
    return StateEnumerationDeclaration.model_validate(
        {
            "schema_version": "merlin.state-enumeration-declaration.v1",
            "name": "character",
            "selector": {
                "schema_version": "merlin.state-selector.v1",
                "step_id": "read-domain",
                "evaluation_root": "response_body",
                "pointer": "/characters",
                "evidence_refs": ["evidence:selector"],
                "confidence": 1.0,
            },
            "shape": shape,
            **({"scope_dimension": "tier"} if shape == "scoped" else {}),
            "completeness": completeness,
            "evidence_refs": ["evidence:enumeration"],
            "confidence": 1.0,
        }
    )


def test_state_capabilities_add_enumerations_without_changing_legacy_shape() -> None:
    legacy = StateCapabilities()
    assert legacy.enumerations == ()
    assert "enumerations" not in state_capabilities_semantic_projection(legacy)

    capabilities = StateCapabilities(
        schema_version="merlin.state-capabilities.v1",
        enumerations=(_declaration(),),
    )
    projection = state_capabilities_semantic_projection(capabilities)

    assert projection["enumerations"] == [
        {
            "schema_version": "merlin.state-enumeration-declaration.v1",
            "name": "character",
            "selector": {
                "schema_version": "merlin.state-selector.v1",
                "step_id": "read-domain",
                "evaluation_root": "response_body",
                "pointer": "/characters",
            },
            "shape": "flat",
            "completeness": "complete",
        }
    ]


def test_versioned_capabilities_require_reviewed_enumeration_authority() -> None:
    raw = _declaration().model_dump(mode="json")
    raw["evidence_refs"] = []
    with pytest.raises(ValidationError, match="versioned state enumerations"):
        StateCapabilities.model_validate(
            {
                "schema_version": "merlin.state-capabilities.v1",
                "enumerations": [raw],
            }
        )


def test_complete_flat_enumeration_builds_canonical_authoritative_receipt() -> None:
    receipt = build_state_enumeration(
        _declaration(),
        selected_value=["a", "b"],
        binding_context={"adapter_digest": "sha256:adapter"},
    )

    assert receipt.authority == "target"
    assert receipt.members_by_scope == {"*": ("a", "b")}
    assert receipt.complete is True
    assert complete_enumeration_members(receipt) == {"*": ("a", "b")}
    assert StateEnumerationReceipt.model_validate(receipt.model_dump(mode="json")) == receipt


def test_scoped_partial_enumeration_is_valid_evidence_but_not_a_denominator() -> None:
    receipt = build_state_enumeration(
        _declaration(shape="scoped", completeness="partial"),
        selected_value={"1": ["a", "b"], "2": ["c"]},
        binding_context={"adapter_digest": "sha256:adapter"},
    )

    assert receipt.scope_dimension == "tier"
    assert receipt.members_by_scope == {"1": ("a", "b"), "2": ("c",)}
    assert receipt.complete is False
    assert complete_enumeration_members(receipt) is None


def test_complete_scoped_empty_enumeration_is_explicit_and_auditable() -> None:
    receipt = build_state_enumeration(
        _declaration(shape="scoped"),
        selected_value={},
        binding_context={"adapter_digest": "sha256:adapter"},
    )

    assert receipt.members_by_scope == {"*": ()}
    assert complete_enumeration_members(receipt) == {"*": ()}


@pytest.mark.parametrize(
    "selected_value",
    [
        "a",
        ["a", "a"],
        ["a", 2],
    ],
)
def test_enumeration_oracle_rejects_nonfinite_or_ambiguous_members(
    selected_value: object,
) -> None:
    with pytest.raises(ValueError):
        build_state_enumeration(
            _declaration(),
            selected_value=selected_value,
            binding_context={"adapter_digest": "sha256:adapter"},
        )


def test_enumeration_receipt_rejects_tampered_members() -> None:
    receipt = build_state_enumeration(
        _declaration(),
        selected_value=["a"],
        binding_context={"adapter_digest": "sha256:adapter"},
    ).model_dump(mode="json")
    receipt["members_by_scope"] = {"*": ["other"]}

    with pytest.raises(ValidationError, match="digest does not match"):
        StateEnumerationReceipt.model_validate(receipt)
