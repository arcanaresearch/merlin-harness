from merlin_agents.arm_crossover import (
    CROSSOVER_OUTPUT_SCHEMA,
    build_crossover_prompt,
    generate_crossovers,
)

_EXISTING_OBJECTIVE = (
    "Test the authentication authorization and tenant isolation boundary by "
    "sending crafted credentials to the reviewed adapter."
)
_NEAR_DUPLICATE = "Test authentication authorization by sending crafted tokens to the adapter."
_NOVEL_OBJECTIVE = "Inspect cross-tenant isolation between two provisioned identities."

_PARENTS = [
    {
        "technique_id": "parent/one",
        "objective": "Probe the tenant isolation boundary with crafted credentials.",
        "semantic_id": "tenant-isolation",
        "strategy_id": "direct",
    },
    {
        "technique_id": "parent/two",
        "objective": "Fuzz the reviewed adapter's input register.",
        "semantic_id": "input-register",
        "strategy_id": "mcp",
    },
]


def test_crossover_prompt_lists_parents_and_existing_objectives() -> None:
    prompt = build_crossover_prompt(
        parents=_PARENTS,
        existing_objectives=[_EXISTING_OBJECTIVE],
        maximum=3,
    )
    assert "recombining" in prompt
    assert "do NOT repeat" in prompt
    assert _EXISTING_OBJECTIVE in prompt
    assert "parent/one" in prompt
    assert "mcp" in prompt
    assert "at most 3 hybrid arms" in prompt


def test_generate_crossovers_prompt_excludes_existing_objectives() -> None:
    captured: list[str] = []

    def generator(prompt: str) -> list[dict]:
        captured.append(prompt)
        return []

    generate_crossovers(
        parents=_PARENTS,
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=3,
    )
    assert captured
    assert _EXISTING_OBJECTIVE in captured[0]
    assert "do NOT repeat" in captured[0]


def test_generate_crossovers_drops_invalid_but_keeps_distinct_similar_arm() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "arm/novel",
                "objective": _NOVEL_OBJECTIVE,
                "semantic_id": "cross-tenant-isolation",
                "strategy_id": "direct",
                "rationale": "hybrid framing",
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

    result = generate_crossovers(
        parents=_PARENTS,
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=5,
    )

    assert [arm["semantic_id"] for arm in result] == [
        "cross-tenant-isolation",
        "auth-boundary",
    ]
    assert all(arm["source"] == "crossover" for arm in result)
    assert result[0]["technique_id"] == "arm/novel"
    assert result[0]["objective"] == _NOVEL_OBJECTIVE
    assert result[0]["rationale"] == "hybrid framing"


def test_generate_crossovers_keeps_similar_objective_for_distinct_identity() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": "arm/similar",
                "objective": _NEAR_DUPLICATE,
                "semantic_id": "similar-cell",
                "strategy_id": "direct",
            }
        ]

    result = generate_crossovers(
        parents=_PARENTS,
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=5,
    )
    assert len(result) == 1
    assert result[0]["semantic_id"] == "similar-cell"


def test_generate_crossovers_normalizes_and_limits_output() -> None:
    def generator(_prompt: str) -> list[dict]:
        return [
            {
                "technique_id": f"arm/{index}",
                "objective": f"Recombine a distinct boundary for case {index}.",
                "semantic_id": f"boundary-{index}",
                "strategy_id": "direct",
            }
            for index in range(5)
        ]

    result = generate_crossovers(
        parents=_PARENTS,
        existing_objectives=[],
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [arm["semantic_id"] for arm in result] == ["boundary-0", "boundary-1"]
    assert all(arm["source"] == "crossover" for arm in result)
    assert "rationale" not in result[0]


def test_generate_crossovers_deduplicates_identical_arm_keys() -> None:
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

    result = generate_crossovers(
        parents=_PARENTS,
        existing_objectives=[],
        generator=generator,
        maximum=5,
    )

    assert [arm["technique_id"] for arm in result] == ["arm/one"]


def test_crossover_output_schema_requires_core_fields() -> None:
    assert CROSSOVER_OUTPUT_SCHEMA["type"] == "object"
    assert set(CROSSOVER_OUTPUT_SCHEMA["required"]) == {
        "technique_id",
        "objective",
        "semantic_id",
        "strategy_id",
    }
