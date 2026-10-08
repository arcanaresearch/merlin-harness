import pytest
from merlin_agents.arm_mutation import (
    MUTATION_OUTPUT_SCHEMA,
    build_mutation_prompt,
    generate_mutations,
)

_EXISTING_OBJECTIVE = (
    "Test the authentication authorization and tenant isolation boundary by "
    "sending crafted credentials to the reviewed adapter."
)
_SOURCE_ARM = {
    "technique_id": "arm/auth-boundary",
    "objective": _EXISTING_OBJECTIVE,
    "semantic_id": "auth-boundary",
    "strategy_id": "direct",
}
_NEAR_DUPLICATE = "Test authentication authorization by sending crafted tokens to the adapter."
_NOVEL_OBJECTIVE = "Inspect cross-tenant isolation between two provisioned identities."


def test_mutation_prompt_includes_source_and_direction() -> None:
    prompt = build_mutation_prompt(
        arm=_SOURCE_ARM,
        existing_objectives=[_EXISTING_OBJECTIVE],
        direction="more aggressive",
        maximum=3,
    )
    assert "more aggressive" in prompt
    assert "arm/auth-boundary" in prompt
    assert _EXISTING_OBJECTIVE in prompt
    assert "do NOT repeat" in prompt
    assert "1 and 3 variants" in prompt


def test_generate_mutations_prompt_excludes_existing_objectives() -> None:
    captured: list[str] = []

    def generator(prompt: str) -> list[dict]:
        captured.append(prompt)
        return []

    generate_mutations(
        arm=_SOURCE_ARM,
        existing_objectives=[_EXISTING_OBJECTIVE],
        direction="encoding",
        generator=generator,
        maximum=3,
    )
    assert captured
    assert _EXISTING_OBJECTIVE in captured[0]
    assert "do NOT repeat" in captured[0]
    assert "encoding" in captured[0]


def test_generate_mutations_drops_invalid_but_keeps_distinct_similar_variant() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "arm/novel",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "cross-tenant-isolation",
                "strategy_id": "encoding",
                "rationale": "shifted the boundary",
            },
            {
                "technique_id": "arm/bad",
                "objective": "",
                "semantic_id": "bad",
                "strategy_id": "direct",
            },
            {
                "technique_id": "arm/dup",
                "objective": _NEAR_DUPLICATE,
                "semantic_id": "auth-boundary",
                "strategy_id": "direct",
            },
        ]

    result = generate_mutations(
        arm=_SOURCE_ARM,
        existing_objectives=[_EXISTING_OBJECTIVE],
        direction="encoding",
        generator=generator,
        maximum=5,
    )

    assert [arm["semantic_id"] for arm in result] == [
        "cross-tenant-isolation",
        "auth-boundary",
    ]
    assert all(arm["source"] == "mutation" for arm in result)
    assert result[0]["technique_id"] == "arm/novel"
    assert result[0]["objective"] == _NOVEL_OBJECTIVE
    assert result[0]["strategy_id"] == "encoding"
    assert result[0]["rationale"] == "shifted the boundary"


def test_generate_mutations_normalizes_and_limits_output() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": f"arm/{index}",
                "objective": f"Probe a distinct boundary for case {index}.",
                "semantic_id": f"boundary-{index}",
                "strategy_id": "direct",
            }
            for index in range(5)
        ]

    result = generate_mutations(
        arm=_SOURCE_ARM,
        existing_objectives=[],
        direction="more aggressive",
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [arm["semantic_id"] for arm in result] == ["boundary-0", "boundary-1"]
    assert all(arm["source"] == "mutation" for arm in result)
    assert "rationale" not in result[0]


def test_generate_mutations_deduplicates_identical_arm_keys() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "arm/one",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
            {
                "technique_id": "arm/one",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
        ]

    result = generate_mutations(
        arm=_SOURCE_ARM,
        existing_objectives=[],
        direction="register",
        generator=generator,
        maximum=5,
    )

    assert [arm["technique_id"] for arm in result] == ["arm/one"]


def test_mutation_output_schema_requires_core_fields() -> None:
    assert MUTATION_OUTPUT_SCHEMA["type"] == "object"
    assert set(MUTATION_OUTPUT_SCHEMA["required"]) == {
        "technique_id",
        "objective",
        "semantic_id",
        "strategy_id",
    }


def test_generate_mutations_rejects_non_positive_maximum() -> None:
    with pytest.raises(ValueError, match="mutation maximum must be positive"):
        build_mutation_prompt(
            arm=_SOURCE_ARM,
            existing_objectives=[],
            direction="encoding",
            maximum=0,
        )
