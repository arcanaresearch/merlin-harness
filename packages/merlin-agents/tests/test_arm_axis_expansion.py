from merlin_agents.arm_axis_expansion import (
    AXIS_VALUE_OUTPUT_SCHEMA,
    build_axis_expansion_prompt,
    generate_axis_values,
)

_EXISTING_FRAGMENT = "Adopt a formal, deferential register toward the target."
_NEAR_DUPLICATE_FRAGMENT = "Adopt a formal and deferential register toward the target system."
_NOVEL_FRAGMENT = "Mimic a technical support representative troubleshooting the device."


def _existing_values() -> list[dict[str, object]]:
    return [
        {
            "axis": "register",
            "slug": "formal",
            "display_name": "Formal",
            "instruction_fragment": _EXISTING_FRAGMENT,
            "obscurity": 3,
        }
    ]


def test_axis_expansion_prompt_mentions_axis_and_existing_values() -> None:
    prompt = build_axis_expansion_prompt(
        axis="register", existing_values=_existing_values(), maximum=3
    )
    assert "register" in prompt
    assert "formal" in prompt
    assert "at most 3" in prompt
    assert "do NOT repeat" in prompt


def test_generate_axis_values_drops_invalid_but_keeps_distinct_similar_slug() -> None:
    def generator(_prompt: str) -> list[dict[str, object]]:
        return [
            {
                "slug": "novel",
                "display_name": "Novel",
                "instruction_fragment": _NOVEL_FRAGMENT,
                "obscurity": 2,
            },
            {"slug": "bad", "display_name": "Bad", "instruction_fragment": ""},
            {
                "slug": "dup",
                "display_name": "Dup",
                "instruction_fragment": _NEAR_DUPLICATE_FRAGMENT,
                "obscurity": 3,
            },
        ]

    result = generate_axis_values(
        axis="register",
        existing_values=_existing_values(),
        generator=generator,
        maximum=5,
    )

    assert [value["slug"] for value in result] == ["novel", "dup"]
    assert all(value["origin"] == "axis-expansion" for value in result)
    assert result[0]["display_name"] == "Novel"
    assert result[0]["instruction_fragment"] == _NOVEL_FRAGMENT
    assert result[0]["obscurity"] == 2


def test_generate_axis_values_defaults_obscurity_and_dedupes_slugs() -> None:
    def generator(_prompt: str) -> list[dict[str, object]]:
        return [
            {
                "slug": "first",
                "display_name": "First",
                "instruction_fragment": _NOVEL_FRAGMENT,
            },
            {
                "slug": "first",
                "display_name": "First again",
                "instruction_fragment": "A completely different fragment about probes.",
            },
            {
                "slug": "formal",
                "display_name": "Formal",
                "instruction_fragment": "Yet another distinct fragment.",
            },
        ]

    result = generate_axis_values(
        axis="register",
        existing_values=_existing_values(),
        generator=generator,
        maximum=5,
    )

    assert [value["slug"] for value in result] == ["first"]
    assert result[0]["obscurity"] == 3


def test_generate_axis_values_caps_at_maximum() -> None:
    fragments = [
        "Pose as a lost traveler asking for directions to a private building.",
        "Imitate a chef describing a proprietary recipe in vivid detail.",
        "Adopt the voice of a historian narrating an alternate timeline.",
        "Mimic a referee explaining the rules of an obscure sport.",
        "Pretend to be a librarian cataloging restricted archival material.",
    ]

    def generator(_prompt: str) -> list[dict[str, object]]:
        return [
            {
                "slug": f"value-{index}",
                "display_name": f"Value {index}",
                "instruction_fragment": fragment,
            }
            for index, fragment in enumerate(fragments)
        ]

    result = generate_axis_values(
        axis="register",
        existing_values=[],
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [value["slug"] for value in result] == ["value-0", "value-1"]


def test_generate_axis_values_keeps_similar_fragment_under_distinct_slug() -> None:
    def generator(_prompt: str) -> list[dict[str, object]]:
        return [
            {
                "slug": "similar",
                "display_name": "Similar",
                "instruction_fragment": "Adopt a formal register toward the target.",
            }
        ]

    result = generate_axis_values(
        axis="register",
        existing_values=_existing_values(),
        generator=generator,
        maximum=5,
    )

    assert [item["slug"] for item in result] == ["similar"]


def test_axis_value_output_schema_requires_core_fields() -> None:
    assert AXIS_VALUE_OUTPUT_SCHEMA["type"] == "object"
    assert set(AXIS_VALUE_OUTPUT_SCHEMA["required"]) == {
        "slug",
        "display_name",
        "instruction_fragment",
    }
