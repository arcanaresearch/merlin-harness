from merlin_agents.arm_brainstorm import (
    BRAINSTORM_OUTPUT_SCHEMA,
    build_brainstorm_prompt,
    generate_arms,
)

_EXISTING_OBJECTIVE = (
    "Test the authentication authorization and tenant isolation boundary by "
    "sending crafted credentials to the reviewed adapter."
)
_NEAR_DUPLICATE = "Test authentication authorization by sending crafted tokens to the adapter."
_NOVEL_OBJECTIVE = "Inspect cross-tenant isolation between two provisioned identities."


def test_brainstorm_prompt_lists_existing_objectives_for_exclusion() -> None:
    prompt = build_brainstorm_prompt(
        seeds=[{"technique_id": "prompt-injection", "reason": "seed reason"}],
        receipts=[{"summary": "Observed a chat input that echoes instructions."}],
        existing_objectives=[_EXISTING_OBJECTIVE],
        maximum=3,
    )
    assert "do NOT repeat" in prompt
    assert _EXISTING_OBJECTIVE in prompt
    assert "prompt-injection" in prompt
    assert "Observed a chat input" in prompt
    assert "at most 3 arms" in prompt


def test_generate_arms_prompt_excludes_existing_objectives() -> None:
    captured: list[str] = []

    def generator(prompt: str) -> list[dict]:
        captured.append(prompt)
        return []

    generate_arms(
        seeds=[],
        receipts=[],
        existing_arms=[{"objective": _EXISTING_OBJECTIVE}],
        generator=generator,
        maximum=3,
    )
    assert captured
    assert _EXISTING_OBJECTIVE in captured[0]
    assert "do NOT repeat" in captured[0]


def test_generate_arms_drops_invalid_but_keeps_distinct_near_duplicate_arms() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "arm/novel",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "cross-tenant-isolation",
                "strategy_id": "direct",
                "rationale": "fresh boundary",
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

    result = generate_arms(
        seeds=[],
        receipts=[],
        existing_arms=[{"objective": _EXISTING_OBJECTIVE}],
        generator=generator,
        maximum=5,
    )

    assert [arm["semantic_id"] for arm in result] == [
        "cross-tenant-isolation",
        "auth-boundary",
    ]
    assert all(arm["source"] == "brainstorm" for arm in result)
    assert result[0]["technique_id"] == "arm/novel"
    assert result[0]["objective"] == _NOVEL_OBJECTIVE
    assert result[0]["rationale"] == "fresh boundary"


def test_generate_arms_normalizes_and_limits_output() -> None:
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

    result = generate_arms(
        seeds=[],
        receipts=[],
        existing_arms=[],
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [arm["semantic_id"] for arm in result] == ["boundary-0", "boundary-1"]
    assert all(arm["source"] == "brainstorm" for arm in result)
    assert "rationale" not in result[0]


def test_generate_arms_deduplicates_identical_arm_keys() -> None:
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
                "objective": "A revised description of the same candidate identity.",
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
            {
                "technique_id": "arm/two",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "same-cell",
                "strategy_id": "direct",
            },
        ]

    result = generate_arms(
        seeds=[],
        receipts=[],
        existing_arms=[],
        generator=generator,
        maximum=5,
    )

    assert [arm["technique_id"] for arm in result] == ["arm/one", "arm/two"]
    assert result[0]["objective"] == _NOVEL_OBJECTIVE


def test_brainstorm_output_schema_requires_core_fields() -> None:
    assert BRAINSTORM_OUTPUT_SCHEMA["type"] == "object"
    assert set(BRAINSTORM_OUTPUT_SCHEMA["required"]) == {
        "technique_id",
        "objective",
        "semantic_id",
        "strategy_id",
    }
