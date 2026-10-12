from dataclasses import replace

from merlin_agents.bounded_children import (
    materialize_bounded_child,
    materialize_bounded_child_family,
)
from merlin_agents.campaign import materialize_language_technique
from merlin_agents.specialists import (
    AssignmentTechnique,
    LanguageCandidateReference,
    LanguageFrontierSpec,
)


def _parent() -> AssignmentTechnique:
    return AssignmentTechnique(
        id="candidate/source@lang=en@variant=native",
        objective="Exercise the reviewed authority-confusion technique.",
        tags=frozenset({"authority"}),
        guidance_ids=("guidance/authority",),
        semantic_id="candidate/source",
        strategy_id="direct",
        transform_id="native",
        language="en",
        domain_id="agent-chat",
        mutation_seed="original semantic payload",
    )


def test_materialize_bounded_child_inherits_parent_and_binds_exact_payload() -> None:
    parent = _parent()
    child = materialize_bounded_child(
        parent,
        {
            "id": parent.id + "@bounded=abc",
            "variant_id": "bounded:abc",
            "variant": {
                "schema": "merlin.bounded-child-candidate.v1",
                "kind": "bounded_child",
                "input_text": "Exact concise payload.",
                "parent_candidate_id": parent.id,
                "parent_identity": {"payload_family": "native"},
                "constraint": {
                    "kind": "string_length",
                    "maximum": 24,
                    "unit": "unicode_characters",
                },
            },
        },
    )

    assert child.id == parent.id + "@bounded=abc"
    assert child.transform_id == "bounded:abc"
    assert child.semantic_id == parent.semantic_id
    assert child.strategy_id == parent.strategy_id
    assert child.language == parent.language
    assert child.guidance_ids == parent.guidance_ids
    assert child.mutation_seed == "Exact concise payload."
    assert child.transform_spec is None
    assert '"Exact concise payload."' in child.objective
    assert "protocol-bounded-child" in child.tags


def test_materialize_bounded_child_rejects_payload_still_over_limit() -> None:
    parent = _parent()
    try:
        materialize_bounded_child(
            parent,
            {
                "id": parent.id + "@bounded=abc",
                "variant_id": "bounded:abc",
                "variant": {
                    "schema": "merlin.bounded-child-candidate.v1",
                    "kind": "bounded_child",
                    "input_text": "ééé",
                    "parent_candidate_id": parent.id,
                    "constraint": {
                        "kind": "string_length",
                        "maximum": 5,
                        "unit": "utf8_bytes",
                    },
                },
            },
        )
    except ValueError as error:
        assert "still violates" in str(error)
    else:
        raise AssertionError("oversized bounded child was admitted")


def test_pre_materialized_adaptive_bounded_child_bypasses_language_remutation() -> None:
    parent = replace(_parent(), strategy_id="goat")
    child = materialize_bounded_child(
        parent,
        {
            "id": parent.id + "@bounded=abc",
            "variant_id": "bounded:abc",
            "variant": {
                "schema": "merlin.bounded-child-candidate.v1",
                "kind": "bounded_child",
                "input_text": "Exact payload.",
                "parent_candidate_id": parent.id,
                "constraint": {
                    "kind": "string_length",
                    "maximum": 20,
                    "unit": "unicode_characters",
                },
            },
        },
    )
    reference = LanguageCandidateReference(
        id=child.id,
        family_id=child.id,
        ordinal=2,
        requested_language="en",
        variant_id="bounded:abc",
    )

    restored = materialize_language_technique(
        child,
        LanguageFrontierSpec(default_language="en", planned_languages=("en",)),
        reference,
    )

    assert restored == child


def test_bounded_child_family_reconstructs_parent_language_cell_from_identity() -> None:
    family = replace(_parent(), id="candidate/source", language="en", transform_id="baseline")
    candidate = {
        "id": "candidate/source@lang=fr@variant=translated@bounded=abc",
        "variant_id": "bounded:abc",
        "variant": {
            "schema": "merlin.bounded-child-candidate.v1",
            "kind": "bounded_child",
            "input_text": "Charge utile exacte.",
            "parent_candidate_id": "candidate/source@lang=fr@variant=translated",
            "parent_identity": {
                "payload_family": "translated",
                "requested_language": "fr",
            },
            "constraint": {
                "kind": "string_length",
                "maximum": 30,
                "unit": "unicode_characters",
            },
        },
    }

    child = materialize_bounded_child_family(family, candidate)

    assert child.id == candidate["id"]
    assert child.language == "fr"
    assert child.transform_id == "bounded:abc"
    assert child.semantic_id == family.id
    assert child.mutation_seed == "Charge utile exacte."
    assert "Language cell: fr" in child.objective
