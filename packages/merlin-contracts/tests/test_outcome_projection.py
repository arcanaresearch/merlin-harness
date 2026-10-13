import pytest
from merlin_contracts import (
    assert_candidate_outcome_match,
    candidate_evidence_digest,
    candidate_id_for_identity,
    model_authority_disagreement,
    project_candidate_outcome,
    summarize_candidate_projections,
    validate_candidate_outcome_projection,
)


def _projection(candidate_id: str, outcome: str = "confirmed") -> dict[str, object]:
    return project_candidate_outcome(
        candidate_id=candidate_id,
        attempt_disposition="valid",
        outcome_authority="target",
        outcome=outcome,
        evidence_digest="a" * 64,
    )


def test_non_valid_candidate_projection_stays_retryable_and_non_authoritative() -> None:
    projection = project_candidate_outcome(
        candidate_id="candidate/one",
        attempt_disposition="contaminated",
        outcome_authority="target",
        outcome="confirmed",
        evidence_digest="b" * 64,
    )

    assert projection["valid"] is False
    assert projection["outcome_authority"] == "none"
    assert projection["outcome"] == "unclear"
    assert projection["terminality"] == "retryable"


def test_model_disagreement_preserves_authority_and_requests_validation() -> None:
    projection = _projection("candidate/one", "refuted")

    disagreement = model_authority_disagreement("confirmed", projection)

    assert disagreement == {
        "schema": "merlin.model-authority-disagreement.v1",
        "candidate_id": "candidate/one",
        "model_outcome": "confirmed",
        "authoritative_outcome": "refuted",
        "evidence_digest": "sha256:" + "a" * 64,
        "escalation": "independent_validation",
        "retryable": True,
    }
    assert projection["outcome"] == "refuted"


def test_candidate_summary_separates_unique_candidates_from_repeat_attempts() -> None:
    summary = summarize_candidate_projections(
        (
            _projection("candidate/one"),
            _projection("candidate/one"),
            _projection("candidate/two", "refuted"),
        )
    )

    assert summary == {
        "attempt_count": 3,
        "unique_candidate_count": 2,
        "repeat_attempt_count": 1,
        "terminal_unique_candidate_count": 2,
        "conflicting_unique_candidate_count": 0,
    }


def test_conflicting_repeats_do_not_count_as_one_terminal_candidate() -> None:
    summary = summarize_candidate_projections(
        (_projection("candidate/one", "confirmed"), _projection("candidate/one", "refuted"))
    )

    assert summary["unique_candidate_count"] == 1
    assert summary["repeat_attempt_count"] == 1
    assert summary["terminal_unique_candidate_count"] == 0
    assert summary["conflicting_unique_candidate_count"] == 1


def test_candidate_identity_and_evidence_relations_are_order_independent() -> None:
    assert candidate_id_for_identity({"technique": "x", "language": "en"}) == (
        candidate_id_for_identity({"language": "en", "technique": "x"})
    )
    assert candidate_evidence_digest(("receipt:b", "receipt:a", "receipt:a")) == (
        candidate_evidence_digest(("receipt:a", "receipt:b"))
    )


def test_projection_validation_rejects_any_canonical_field_drift() -> None:
    authoritative = _projection("candidate/one")
    assert validate_candidate_outcome_projection(authoritative) == authoritative
    for field, value in (
        ("candidate_id", "candidate/two"),
        ("valid", False),
        ("outcome", "refuted"),
        ("evidence_digest", "sha256:" + "b" * 64),
        ("terminality", "retryable"),
    ):
        with pytest.raises(ValueError):
            assert_candidate_outcome_match(authoritative, {**authoritative, field: value})
