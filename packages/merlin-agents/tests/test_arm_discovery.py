import pytest
from merlin_agents.arm_discovery import ArmDiscoveryResult, discover_arms

_TARGET_ID = "target-1"
_SCENARIO = "sha256:scenario"


def _arm(
    technique_id: str, objective: str, semantic_id: str, strategy_id: str = "direct"
) -> dict[str, object]:
    return {
        "technique_id": technique_id,
        "objective": objective,
        "semantic_id": semantic_id,
        "strategy_id": strategy_id,
    }


def _brainstorm_generator(prompt: str) -> list[dict[str, object]]:
    assert "proposing novel assessment arms" in prompt
    return [
        {
            "technique_id": "arm/brainstorm",
            "objective": "Probe cross-tenant isolation between provisioned identities.",
            "semantic_id": "cross-tenant-isolation",
            "strategy_id": "direct",
            "rationale": "fresh boundary",
            "mechanism_claim": "cross-tenant object reference confusion",
            "trust_boundary": "tenant A to tenant B object ownership",
            "expected_effect": "tenant B case data is reflected to tenant A",
            "falsification_test": "repeat against a patched ownership check",
            "closest_known_technique_id": "catalogue/idor",
        }
    ]


def test_discover_arms_runs_brainstorm_and_records_enriched_arms() -> None:
    recorded: list[dict[str, object]] = []

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        seeds=[{"technique_id": "prompt", "reason": "seed"}],
        receipts=[{"summary": "observed echo"}],
        existing_arms=[],
        generator=_brainstorm_generator,
        maximum=3,
        record_arm=recorded.append,
    )

    assert isinstance(result, ArmDiscoveryResult)
    assert len(result.admitted) == 1
    assert result.admitted[0]["source"] == "brainstorm"
    arm = result.admitted[0]
    assert arm["id"].startswith("arm:")
    assert arm["target_id"] == _TARGET_ID
    assert arm["scenario_fingerprint"] == _SCENARIO
    assert arm["source"] == "brainstorm"
    assert arm["status"] == "trial"
    assert arm["novelty_digest"].startswith("sha256:")
    assert arm["semantic_id"] == "cross-tenant-isolation"
    assert arm["strategy_id"] == "direct"
    assert arm["rationale"] == "fresh boundary"
    assert arm["mechanism_claim"] == "cross-tenant object reference confusion"
    assert arm["trust_boundary"] == "tenant A to tenant B object ownership"
    assert arm["expected_effect"] == "tenant B case data is reflected to tenant A"
    assert arm["falsification_test"] == "repeat against a patched ownership check"
    assert arm["closest_known_technique_id"] == "catalogue/idor"
    assert result.pool == result.admitted
    assert recorded == list(result.admitted)


def test_discover_arms_interleaves_full_catalog_and_brainstorm_pools() -> None:
    prompts: list[str] = []

    def generator(prompt: str) -> list[dict[str, object]]:
        prompts.append(prompt)
        return [
            _arm(
                f"brainstorm/{index}",
                f"Probe target-derived boundary {index}.",
                f"target-boundary-{index}",
            )
            for index in range(10)
        ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        seeds=[
            {
                "technique_id": "cat/direct/override-baseline",
                "semantic_id": "cat/direct/override-baseline",
                "objective": "Test the reviewed target policy boundary.",
                "execution_role": "direct",
                "tags": ("prompt-injection",),
            }
        ],
        receipts=[{"summary": "observed tenant boundary"}],
        existing_arms=(),
        generator=generator,
        maximum=10,
    )

    sources = [arm["source"] for arm in result.admitted]
    assert sources.count("catalog-mutation") == 5
    assert sources.count("brainstorm") == 5
    assert "Propose at most 10 arms" in prompts[0]


def test_discover_arms_runs_failure_and_win_generators_when_signalled() -> None:
    def generator(prompt: str) -> list[dict[str, object]]:
        if "route around that failure" in prompt:
            return [
                _arm(
                    "arm/failure",
                    "Route around the refused credential handoff with an alternate channel.",
                    "failure-route",
                )
            ]
        if "successful jailbreak" in prompt:
            return [
                {
                    **_arm(
                        "arm/win",
                        "Generalize the successful rhetorical reframe into a reusable arm.",
                        "win-generalization",
                    ),
                    "axis_coords": {"register": "imperative", "framing": None},
                }
            ]
        return [
            _arm(
                "arm/brainstorm",
                "Probe cross-tenant isolation between provisioned identities.",
                "cross-tenant-isolation",
            )
        ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=[],
        generator=generator,
        maximum=3,
        failure={"transcript": "refused", "failure_reason": "blocked", "technique": "prompt"},
        win={"transcript": "jailbreak succeeded", "strategy": "direct", "technique": "prompt"},
    )

    assert {arm["source"] for arm in result.admitted} == {
        "brainstorm",
        "failure-derived",
        "win-generalization",
    }
    win_arm = next(arm for arm in result.admitted if arm["source"] == "win-generalization")
    assert win_arm["axis_coords"] == {"register": "imperative"}


def test_enrichment_preserves_execution_reachability() -> None:
    from merlin_agents.arm_discovery import _enrich_arm

    arm = _arm("arm/adaptive", "Execute a reachable register", "adaptive")
    arm["execution_reachability"] = {
        "register": "parseltongue",
        "requested_transform_id": "register/wenyanwen",
        "requested_language": "lzh",
        "receipt_required": True,
    }
    arm["controller_id"] = "crescendo"
    arm["mutation_id"] = "register/wenyanwen"
    enriched = _enrich_arm(arm, _TARGET_ID, _SCENARIO)
    assert enriched["execution_reachability"] == arm["execution_reachability"]
    assert enriched["controller_id"] == "crescendo"
    assert enriched["mutation_id"] == "register/wenyanwen"
    assert enriched["requested_language"] == "lzh"

    other_language = _enrich_arm(
        {
            **arm,
            "requested_language": "la",
            "execution_reachability": {
                **arm["execution_reachability"],
                "requested_language": "la",
            },
        },
        _TARGET_ID,
        _SCENARIO,
    )
    assert other_language["id"] != enriched["id"]


def test_enrichment_preserves_ranking_signals_and_structured_uncertainty() -> None:
    from merlin_agents.arm_discovery import _enrich_arm

    arm = {
        **_arm("arm/adaptive", "Try a receipt-derived neighbour", "adaptive"),
        "controller_id": "goat",
        "mutation_id": "register/parseltongue",
        "requested_language": "lzh",
        "generalization_opportunity": 0.75,
        "boundary_opportunity": 0.5,
        "rank_components": {
            "coverage": {"measured_value": 1.0, "reason": "unresolved language cell"}
        },
        "evaluation_uncertainties": (
            {
                "kind": "evaluation_uncertain",
                "source": "language-observer",
                "reason": "requested and observed language disagree",
                "confidence": 0.7,
            },
        ),
    }

    enriched = _enrich_arm(arm, _TARGET_ID, _SCENARIO)

    assert enriched["generalization_opportunity"] == 0.75
    assert enriched["boundary_opportunity"] == 0.5
    assert enriched["rank_components"] == arm["rank_components"]
    assert enriched["evaluation_uncertainties"] == arm["evaluation_uncertainties"]
    assert isinstance(enriched["evaluation_uncertainties"][0], dict)


def test_discover_arms_deduplicates_across_generators() -> None:
    def generator(prompt: str) -> list[dict[str, object]]:
        if "route around that failure" in prompt:
            return [
                _arm(
                    "arm/brainstorm",
                    "Probe cross-tenant isolation between provisioned identities.",
                    "cross-tenant-isolation",
                )
            ]
        return [
            _arm(
                "arm/brainstorm",
                "Probe cross-tenant isolation between provisioned identities.",
                "cross-tenant-isolation",
            )
        ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=[],
        generator=generator,
        maximum=3,
        failure={"transcript": "refused", "failure_reason": "blocked", "technique": "prompt"},
    )

    assert len(result.admitted) == 1


def test_discovery_keeps_distinct_techniques_with_shared_semantic_identity() -> None:
    def generator(_prompt: str) -> list[dict[str, object]]:
        return [
            _arm("arm/first", "Probe the first transport path.", "shared-semantic"),
            _arm("arm/second", "Probe the second transport path.", "shared-semantic"),
        ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=(),
        generator=generator,
        maximum=2,
    )

    assert {arm["technique_id"] for arm in result.admitted} == {
        "arm/first",
        "arm/second",
    }
    assert len({arm["id"] for arm in result.admitted}) == 2


def test_discover_arms_keeps_same_wording_for_distinct_execution_identity() -> None:
    def generator(prompt: str) -> list[dict[str, object]]:
        if "route around that failure" in prompt:
            return [
                _arm(
                    "arm/failure",
                    "Probe cross-tenant isolation between provisioned identities.",
                    "cross-tenant-isolation",
                    "crescendo",
                )
            ]
        return [
            _arm(
                "arm/brainstorm",
                "Probe cross-tenant isolation between provisioned identities.",
                "cross-tenant-isolation",
            )
        ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=[],
        generator=generator,
        maximum=3,
        failure={"transcript": "refused", "failure_reason": "blocked", "technique": "prompt"},
    )

    assert len(result.admitted) == 2
    assert {arm["controller_id"] for arm in result.admitted} == {"direct", "crescendo"}
    duplicate = next(arm for arm in result.admitted if arm["controller_id"] == "crescendo")
    assert duplicate["novelty_observation"]["verdict"] == "duplicate"
    assert duplicate["novelty_observation"]["admission_effect"] == "none"


def test_discover_arms_gates_against_existing_pool() -> None:
    existing = [
        _arm(
            "arm/brainstorm",
            "Probe cross-tenant isolation between provisioned identities.",
            "cross-tenant-isolation",
        )
    ]

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=existing,
        generator=_brainstorm_generator,
        maximum=3,
    )

    assert result.admitted == ()
    assert result.pool == tuple(existing)


def test_discover_arms_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        discover_arms(
            target_id=_TARGET_ID,
            scenario_fingerprint=_SCENARIO,
            generator=_brainstorm_generator,
            maximum=0,
        )
    with pytest.raises(ValueError):
        discover_arms(
            target_id="",
            scenario_fingerprint=_SCENARIO,
            generator=_brainstorm_generator,
            maximum=1,
        )


def test_discover_arms_projection_serializes_pool() -> None:
    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        existing_arms=[],
        generator=_brainstorm_generator,
        maximum=3,
    )
    projection = result.projection()
    assert len(projection["admitted"]) == 1
    assert projection["pool"] == projection["admitted"]


def test_discover_arms_reports_unparseable_model_replies_and_keeps_catalog_floor() -> None:
    from merlin_contracts import ModelJsonError

    def generator(_prompt: str) -> list[dict[str, object]]:
        raise ModelJsonError("model reply is not parseable JSON (Expecting value); reply head: ''")

    result = discover_arms(
        target_id=_TARGET_ID,
        scenario_fingerprint=_SCENARIO,
        seeds=[
            {
                "technique_id": "cat/direct/override-baseline",
                "semantic_id": "cat/direct/override-baseline",
                "objective": "Test the reviewed target policy boundary.",
                "execution_role": "direct",
                "tags": ("prompt-injection",),
            }
        ],
        receipts=[{"summary": "observed echo"}],
        existing_arms=[],
        generator=generator,
        maximum=3,
        failure={"transcript": "refused", "failure_reason": "refusal", "technique": "direct"},
        win={"transcript": "bypassed", "strategy": "reframe", "technique": "direct"},
    )

    # Provider noise in the model-backed generators degrades to zero
    # brainstorm/failure/win candidates; catalog mutations still flow.
    assert result.admitted
    assert all(arm["source"] == "catalog-mutation" for arm in result.admitted)
    assert {item["generator"] for item in result.generation_failures} == {
        "brainstorm",
        "failure-derived",
        "win-generalization",
    }
    assert all(item["status"] == "retryable" for item in result.generation_failures)


def test_discover_arms_still_raises_structural_generator_errors() -> None:
    def generator(_prompt: str) -> list[dict[str, object]]:
        raise RuntimeError("durable store unavailable")

    with pytest.raises(RuntimeError, match="durable store unavailable"):
        discover_arms(
            target_id=_TARGET_ID,
            scenario_fingerprint=_SCENARIO,
            seeds=[],
            receipts=[],
            existing_arms=[],
            generator=generator,
            maximum=3,
        )
