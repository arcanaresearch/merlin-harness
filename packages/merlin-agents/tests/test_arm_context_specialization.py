from merlin_agents.arm_context_specialization import (
    SPECIALIZATION_OUTPUT_SCHEMA,
    build_specialization_prompt,
    generate_specializations,
)

_SOURCE_ARM = {
    "technique_id": "prompt-injection",
    "objective": (
        "Inject a system-role override into the chat input and observe whether "
        "the adapter still honors its developer instructions."
    ),
    "semantic_id": "prompt-injection",
    "strategy_id": "direct",
    "rationale": "Worked on the chatbot surface.",
}

_EXISTING_OBJECTIVE = (
    "Test the authentication authorization and tenant isolation boundary by "
    "sending crafted credentials to the reviewed adapter."
)
_NEAR_DUPLICATE = "Test authentication authorization by sending crafted tokens to the adapter."
_NOVEL_OBJECTIVE = "Inject a system-role override through the tenant-scoped API surface."


def test_specialization_prompt_names_arm_context_and_exclusions() -> None:
    prompt = build_specialization_prompt(
        arm=_SOURCE_ARM,
        target_context="tenant isolation",
        existing_objectives=[_EXISTING_OBJECTIVE],
        maximum=3,
    )
    assert "do NOT repeat" in prompt
    assert "tenant isolation" in prompt
    assert "prompt-injection" in prompt
    assert "Inject a system-role override" in prompt
    assert _EXISTING_OBJECTIVE in prompt
    assert "at most 3 specialized arms" in prompt


def test_generate_specializations_prompt_includes_exclusions() -> None:
    captured: list[str] = []

    def generator(prompt: str) -> list[dict]:
        captured.append(prompt)
        return []

    generate_specializations(
        arm=_SOURCE_ARM,
        target_context="language localization",
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=3,
    )
    assert captured
    assert _EXISTING_OBJECTIVE in captured[0]
    assert "do NOT repeat" in captured[0]
    assert "language localization" in captured[0]


def test_generate_specializations_drops_invalid_but_keeps_distinct_similar_cells() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "specialized/novel",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "tenant-prompt-injection",
                "strategy_id": "direct",
                "rationale": "adapted for tenants",
            },
            {
                "technique_id": "specialized/bad",
                "objective": "",
                "semantic_id": "bad",
                "strategy_id": "direct",
            },
            {
                "technique_id": "specialized/dup",
                "objective": _NEAR_DUPLICATE,
                "semantic_id": "auth-boundary",
                "strategy_id": "direct",
            },
        ]

    result = generate_specializations(
        arm=_SOURCE_ARM,
        target_context="tenant isolation",
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=5,
    )

    assert [arm["semantic_id"] for arm in result] == [
        "tenant-prompt-injection",
        "auth-boundary",
    ]
    assert all(arm["source"] == "contextual" for arm in result)
    assert result[0]["technique_id"] == "specialized/novel"
    assert result[0]["objective"] == _NOVEL_OBJECTIVE
    assert result[0]["rationale"] == "adapted for tenants"


def test_generate_specializations_normalizes_and_limits_output() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": f"specialized/{index}",
                "objective": f"Probe a distinct tenant boundary for case {index}.",
                "semantic_id": f"tenant-{index}",
                "strategy_id": "direct",
            }
            for index in range(5)
        ]

    result = generate_specializations(
        arm=_SOURCE_ARM,
        target_context="tenant isolation",
        existing_objectives=[],
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [arm["semantic_id"] for arm in result] == ["tenant-0", "tenant-1"]
    assert all(arm["source"] == "contextual" for arm in result)
    assert "rationale" not in result[0]


def test_generate_specializations_deduplicates_identical_arm_keys() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "specialized/one",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
            {
                "technique_id": "specialized/one",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
        ]

    result = generate_specializations(
        arm=_SOURCE_ARM,
        target_context="tenant isolation",
        existing_objectives=[],
        generator=generator,
        maximum=5,
    )

    assert [arm["technique_id"] for arm in result] == ["specialized/one"]


def test_specialization_schema_requires_core_fields() -> None:
    assert SPECIALIZATION_OUTPUT_SCHEMA["type"] == "object"
    assert set(SPECIALIZATION_OUTPUT_SCHEMA["required"]) == {
        "technique_id",
        "objective",
        "semantic_id",
        "strategy_id",
    }
