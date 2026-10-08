from merlin_agents.arm_failure_derived import (
    build_failure_prompt,
    generate_failure_arms,
)
from merlin_agents.discovery import check_novelty

_EXISTING_OBJECTIVE = (
    "Test the authentication authorization and tenant isolation boundary by "
    "sending crafted credentials to the reviewed adapter."
)
_DUPLICATE_OBJECTIVE = _EXISTING_OBJECTIVE
_SIMILAR_OBJECTIVE = "Test authentication authorization by sending crafted tokens to the adapter."
_NOVEL_OBJECTIVE = "Inspect cross-tenant isolation between two provisioned identities."
_TRANSCRIPT = "Attempted the credential replay and was blocked by the WAF after one request."
_FAILURE_REASON = "The WAF dropped every crafted request before it reached the adapter."
_TECHNIQUE = "credential-replay"


def test_failure_prompt_routes_around_failure_and_lists_objectives() -> None:
    prompt = build_failure_prompt(
        transcript=_TRANSCRIPT,
        failure_reason=_FAILURE_REASON,
        technique=_TECHNIQUE,
        existing_objectives=[_EXISTING_OBJECTIVE],
        maximum=3,
    )
    assert "route around" in prompt
    assert _TECHNIQUE in prompt
    assert _FAILURE_REASON in prompt
    assert _TRANSCRIPT in prompt
    assert "do NOT repeat" in prompt
    assert _EXISTING_OBJECTIVE in prompt
    assert "at most 3 arms" in prompt


def test_failure_prompt_bounds_long_transcript_and_reason() -> None:
    prompt = build_failure_prompt(
        transcript="x" * 50_000,
        failure_reason="y" * 50_000,
        technique="z" * 50_000,
        existing_objectives=[],
        maximum=2,
    )
    assert "x" * 12_000 not in prompt
    assert "\u2026" in prompt


def test_failure_prompt_rejects_non_positive_maximum() -> None:
    try:
        build_failure_prompt(
            transcript=_TRANSCRIPT,
            failure_reason=_FAILURE_REASON,
            technique=_TECHNIQUE,
            existing_objectives=[],
            maximum=0,
        )
    except ValueError as error:
        assert "positive" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_generate_failure_arms_prompt_excludes_existing_objectives() -> None:
    captured: list[str] = []

    def generator(prompt: str) -> list[dict]:
        captured.append(prompt)
        return []

    generate_failure_arms(
        transcript=_TRANSCRIPT,
        failure_reason=_FAILURE_REASON,
        technique=_TECHNIQUE,
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=3,
    )
    assert captured
    assert _EXISTING_OBJECTIVE in captured[0]
    assert "do NOT repeat" in captured[0]
    assert "route around" in captured[0]


def test_generate_failure_arms_drops_invalid_but_keeps_distinct_similar_cells() -> None:
    assert check_novelty(_DUPLICATE_OBJECTIVE, [_EXISTING_OBJECTIVE]).verdict == "duplicate"
    assert check_novelty(_SIMILAR_OBJECTIVE, [_EXISTING_OBJECTIVE]).verdict == "similar"

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
                "objective": _DUPLICATE_OBJECTIVE,
                "semantic_id": "auth-boundary",
                "strategy_id": "direct",
            },
            {
                "technique_id": "arm/similar",
                "objective": _SIMILAR_OBJECTIVE,
                "semantic_id": "auth-tokens",
                "strategy_id": "direct",
            },
        ]

    result = generate_failure_arms(
        transcript=_TRANSCRIPT,
        failure_reason=_FAILURE_REASON,
        technique=_TECHNIQUE,
        existing_objectives=[_EXISTING_OBJECTIVE],
        generator=generator,
        maximum=5,
    )

    assert [arm["semantic_id"] for arm in result] == [
        "cross-tenant-isolation",
        "auth-boundary",
        "auth-tokens",
    ]
    assert all(arm["source"] == "failure-derived" for arm in result)
    assert result[0]["technique_id"] == "arm/novel"
    assert result[0]["objective"] == _NOVEL_OBJECTIVE
    assert result[0]["rationale"] == "fresh boundary"


def test_generate_failure_arms_normalizes_and_limits_output() -> None:
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

    result = generate_failure_arms(
        transcript=_TRANSCRIPT,
        failure_reason=_FAILURE_REASON,
        technique=_TECHNIQUE,
        existing_objectives=[],
        generator=generator,
        maximum=2,
    )

    assert len(result) == 2
    assert [arm["semantic_id"] for arm in result] == ["boundary-0", "boundary-1"]
    assert all(arm["source"] == "failure-derived" for arm in result)
    assert "rationale" not in result[0]


def test_generate_failure_arms_deduplicates_identical_arm_keys() -> None:
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

    result = generate_failure_arms(
        transcript=_TRANSCRIPT,
        failure_reason=_FAILURE_REASON,
        technique=_TECHNIQUE,
        existing_objectives=[],
        generator=generator,
        maximum=5,
    )

    assert [arm["technique_id"] for arm in result] == ["arm/one"]


def test_generate_failure_arms_rejects_non_positive_maximum() -> None:
    try:
        generate_failure_arms(
            transcript=_TRANSCRIPT,
            failure_reason=_FAILURE_REASON,
            technique=_TECHNIQUE,
            existing_objectives=[],
            generator=lambda _prompt: [],
            maximum=0,
        )
    except ValueError as error:
        assert "positive" in str(error)
    else:
        raise AssertionError("expected ValueError")
