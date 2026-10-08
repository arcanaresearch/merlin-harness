import pytest
from merlin_assessments.neighbours import (
    CaseRelationship,
    ObservedCaseReceipt,
    derive_case_relationships,
    receipt_from_mapping,
)


def _receipt(
    case_id: str,
    endpoint: str,
    *parameters: str,
    failure_cause: str | None = None,
) -> ObservedCaseReceipt:
    return ObservedCaseReceipt(
        case_id=case_id,
        endpoint=endpoint,
        parameters=parameters,
        failure_cause=failure_cause,
        evidence_refs=(f"receipt:{case_id}",),
    )


def test_shared_surface_relationship_is_derived_from_matching_endpoints() -> None:
    relationships = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/refund", "order_id"),
            _receipt("pack/b", "https://t.example/refund", "session"),
        )
    )

    assert len(relationships) == 1
    (relationship,) = relationships
    assert relationship.kind == "shared-surface"
    assert relationship.case_ids == ("pack/a", "pack/b")
    assert relationship.basis == "endpoint:https://t.example/refund"
    assert relationship.evidence_refs == ("receipt:pack/a", "receipt:pack/b")
    assert relationship.material_difference.strip()
    assert relationship.id.startswith("sha256:")


def test_shared_surface_relationship_is_derived_from_parameter_overlap() -> None:
    relationships = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/one", "order_id", "total"),
            _receipt("pack/b", "https://t.example/two", "total"),
        )
    )

    assert [item.kind for item in relationships] == ["shared-surface"]
    assert relationships[0].basis == "parameter:total"


def test_shared_failure_cause_relationship_links_same_observed_signature() -> None:
    relationships = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/one", failure_cause="oracle:policy-refund"),
            _receipt("pack/b", "https://t.example/two", failure_cause="oracle:policy-refund"),
        )
    )

    assert [item.kind for item in relationships] == ["shared-failure-cause"]
    relationship = relationships[0]
    assert relationship.basis == "failure-cause:oracle:policy-refund"
    assert "distinct" in relationship.material_difference


def test_cases_without_shared_observations_are_not_linked() -> None:
    relationships = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/one", "alpha", failure_cause="oracle:x"),
            _receipt("pack/b", "https://t.example/two", "beta", failure_cause="oracle:y"),
        )
    )

    assert relationships == ()


def test_derivation_is_deterministic_regardless_of_receipt_order() -> None:
    receipts = (
        _receipt("pack/a", "https://t.example/refund", failure_cause="oracle:x"),
        _receipt("pack/b", "https://t.example/refund", failure_cause="oracle:x"),
        _receipt("pack/c", "https://t.example/other"),
    )
    forward = derive_case_relationships(receipts)
    reverse = derive_case_relationships(tuple(reversed(receipts)))

    assert forward == reverse
    assert tuple(item.id for item in forward) == tuple(sorted(item.id for item in forward))
    assert {item.kind for item in forward} == {"shared-surface", "shared-failure-cause"}


def test_relationship_id_is_content_bound() -> None:
    (relationship,) = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/refund"),
            _receipt("pack/b", "https://t.example/refund"),
        )
    )
    with pytest.raises(ValueError, match="canonical digest"):
        CaseRelationship(
            id="sha256:" + "0" * 64,
            kind=relationship.kind,
            case_ids=relationship.case_ids,
            basis=relationship.basis,
            material_difference=relationship.material_difference,
            evidence_refs=relationship.evidence_refs,
        )


def test_relationships_require_a_material_difference_explanation() -> None:
    (relationship,) = derive_case_relationships(
        (
            _receipt("pack/a", "https://t.example/refund"),
            _receipt("pack/b", "https://t.example/refund"),
        )
    )
    with pytest.raises(ValueError, match="material-difference"):
        CaseRelationship(
            id=relationship.id,
            kind=relationship.kind,
            case_ids=relationship.case_ids,
            basis=relationship.basis,
            material_difference="  ",
            evidence_refs=relationship.evidence_refs,
        )


def test_duplicate_case_receipts_are_rejected() -> None:
    with pytest.raises(ValueError, match="one receipt per case"):
        derive_case_relationships(
            (
                _receipt("pack/a", "https://t.example/one"),
                _receipt("pack/a", "https://t.example/two"),
            )
        )


def test_receipt_validation_rejects_blank_and_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="case id"):
        _receipt(" ", "https://t.example/one")
    with pytest.raises(ValueError, match="endpoint"):
        _receipt("pack/a", " ")
    with pytest.raises(ValueError, match="parameters"):
        _receipt("pack/a", "https://t.example/one", "x", "x")
    with pytest.raises(ValueError, match="failure cause"):
        _receipt("pack/a", "https://t.example/one", failure_cause=" ")


def test_receipt_round_trips_through_its_durable_projection() -> None:
    receipt = _receipt("pack/a", "https://t.example/one", "order_id", failure_cause="oracle:x")
    assert receipt_from_mapping(receipt.projection()) == receipt
    with pytest.raises(ValueError, match="missing required fields"):
        receipt_from_mapping({"case_id": "pack/a"})
    with pytest.raises(ValueError, match="unexpected fields"):
        receipt_from_mapping({**receipt.projection(), "extra": "nope"})
