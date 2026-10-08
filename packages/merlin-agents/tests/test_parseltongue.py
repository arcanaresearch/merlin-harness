from dataclasses import asdict

import pytest
from merlin_agents.adaptive_catalog import adaptive_mutation_catalog
from merlin_agents.parseltongue import (
    ParseltongueExecution,
    audit_parseltongue_catalog,
    execute_parseltongue,
)


def test_all_222_catalogue_entries_have_honest_reachability() -> None:
    catalog = adaptive_mutation_catalog()
    audit = audit_parseltongue_catalog(catalog["parseltongue_transforms"])

    assert len(audit) == 222
    assert len({item.transform_id for item in audit}) == 222
    assert any(item.executable for item in audit)
    assert any(not item.executable for item in audit)
    assert all((item.executor_id is not None) == item.executable for item in audit)
    assert all(item.reason_class != "unreviewed" for item in audit)
    assert sum(item.production_reachable for item in audit) == 222
    preservation_unknown = tuple(
        item for item in audit if item.required_capabilities == ("unicode_tag_preservation",)
    )
    assert len(preservation_unknown) == 3
    assert all(item.production_reachable for item in preservation_unknown)
    assert all(
        item.reason_class == "transport-preservation-unverified" for item in preservation_unknown
    )


def test_exact_transform_execution_is_receipt_bound() -> None:
    result = execute_parseltongue(
        "policy boundary",
        "encoding/base64",
        requested_language="en",
        actual_language="en",
    )

    assert result.output == "cG9saWN5IGJvdW5kYXJ5"
    assert result.requested_transform_id == result.applied_transform_id == "encoding/base64"
    assert result.requested_language == result.actual_language == "en"
    assert ParseltongueExecution(**asdict(result)) == result
    with pytest.raises(ValueError, match="differs from request"):
        ParseltongueExecution(**{**asdict(result), "applied_transform_id": "encoding/hex"})
    with pytest.raises(ValueError, match="not locally executable"):
        execute_parseltongue("source", "cipher/adfgvx")


def test_visible_adversarial_transform_is_reproducible() -> None:
    first = execute_parseltongue("A safe scope", "concealment/homoglyph")
    second = execute_parseltongue("A safe scope", "concealment/homoglyph")

    assert first == second
    assert first.output != "A safe scope"


@pytest.mark.parametrize(
    ("transform_id", "source", "expected"),
    (
        ("case/camel-case", "Hello, SAFE world", "helloSafeWorld"),
        ("case/uppercase-lowercase", "Ab C", "aB c"),
        ("format/numbers-only", "a1 b٢", "1٢"),
        ("format/remove-accents", "café", "cafe"),
        ("format/remove-consonants", "Policy 42", "oi 42"),
        ("format/remove-tabs", "a\tb", "ab"),
        ("format/remove-zero-width", "a\u200bb\ufeffc", "abc"),
    ),
)
def test_additional_low_risk_executors_have_golden_outputs(
    transform_id: str, source: str, expected: str
) -> None:
    assert execute_parseltongue(source, transform_id).output == expected
