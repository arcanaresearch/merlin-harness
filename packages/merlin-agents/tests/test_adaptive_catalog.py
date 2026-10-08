import hashlib
import json

from merlin_agents.adaptive_catalog import (
    adaptive_catalog_audit,
    adaptive_mutation_catalog,
    catalog_mutation_candidates,
)


def test_adaptive_catalog_preserves_full_reviewed_register_and_parseltongue_inventory() -> None:
    catalog = adaptive_mutation_catalog()

    assert len(catalog["registers"]) == 54
    assert len(catalog["parseltongue_transforms"]) == 222
    assert any(item["slug"] == "wenyanwen" for item in catalog["registers"])
    assert any(item["id"] == "encoding/base64" for item in catalog["parseltongue_transforms"])
    audit = adaptive_catalog_audit()
    assert audit.executable_parseltongue_count == 41
    assert audit.non_executable_parseltongue_count == 181
    assert audit.production_eligible_parseltongue_count == 222
    assert audit.capability_blocked_parseltongue_count == 0
    assert (
        hashlib.sha256(
            json.dumps(
                catalog["registers"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        == "45c25fb5b1ba78d86f1a0fc066097e3cbb95510c82771a909f892f1a507363ab"
    )
    assert (
        hashlib.sha256(
            json.dumps(
                catalog["parseltongue_transforms"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        == "af511c49d03d4eff9c1460ffe49e4b4a3b0baecc402dfe42878f28b18d1b2f5c"
    )


def test_catalog_mutations_balance_register_and_encoding_without_hidden_payloads() -> None:
    seeds = (
        {
            "technique_id": "cat/direct/override-baseline",
            "semantic_id": "cat/direct/override-baseline",
            "objective": "Long worker guidance that must not be encoded. " * 100,
            "mutation_seed": "Test the reviewed target policy boundary.",
            "max_target_exchanges": 4,
            "execution_role": "direct",
            "tags": ("prompt-injection",),
        },
    )

    first = catalog_mutation_candidates(seeds, (), maximum=6)
    assert len(first) == 6
    assert {next(iter(item["axis_coords"])) for item in first} == {"register", "encoding"}
    assert all("concealment/invisible-text" not in item["strategy_id"] for item in first)
    assert all(item["source"] == "catalog-mutation" for item in first)
    assert all(item["execution_reachability"] for item in first)
    encoding = next(item for item in first if "encoding" in item["axis_coords"])
    assert encoding["axis_coords"]["encoding"] == "encoding/base64"
    evidence = encoding["execution_reachability"]
    assert evidence["requested_transform_id"] == evidence["prepared_transform_id"]
    assert evidence["applied_transform_id"] is None
    assert evidence["requested_language"] == "und"
    assert evidence["actual_language"] is None
    assert evidence["output"] in encoding["objective"]
    assert "Long worker guidance" not in str(evidence["output"])
    assert encoding["execution_bounds"] == {"max_target_exchanges": 4}

    second = catalog_mutation_candidates(seeds, first, maximum=6)
    assert len(second) == 6
    assert {item["strategy_id"] for item in first}.isdisjoint(
        item["strategy_id"] for item in second
    )


def test_production_schedule_reaches_rare_and_adversarial_mutations_in_two_waves() -> None:
    seeds = (
        {
            "technique_id": "policy-boundary@lang=am@variant=native",
            "semantic_id": "policy-boundary",
            "objective": "Test the reviewed target policy boundary.",
            "execution_role": "direct",
            "tags": ("prompt-injection",),
            "language": "am",
        },
    )

    first = catalog_mutation_candidates(seeds, (), maximum=2)
    second = catalog_mutation_candidates(seeds, first, maximum=2)

    assert {item["strategy_id"] for item in first} == {
        "mutation:register:amharic",
        "mutation:encoding:encoding/base64",
    }
    assert {item["strategy_id"] for item in second} == {
        "mutation:register:wenyanwen",
        "mutation:encoding:concealment/homoglyph",
    }
    encoded = next(item for item in first if "encoding" in item["axis_coords"])
    assert encoded["execution_reachability"]["requested_language"] == "am"
    assert encoded["execution_reachability"]["actual_language"] is None


def test_legacy_language_omission_does_not_collapse_a_new_language_cell() -> None:
    seeds = (
        {
            "technique_id": "policy-boundary@lang=am@variant=native",
            "semantic_id": "policy-boundary",
            "objective": "Test the reviewed target policy boundary.",
            "execution_role": "direct",
            "tags": ("prompt-injection",),
            "language": "am",
        },
    )
    legacy = (
        {
            "semantic_id": "policy-boundary",
            "strategy_id": "mutation:encoding:encoding/base64",
        },
    )

    candidates = catalog_mutation_candidates(seeds, legacy, maximum=2)

    assert "mutation:encoding:encoding/base64" in {str(item["strategy_id"]) for item in candidates}
    base64_arm = next(
        item for item in candidates if item["strategy_id"] == "mutation:encoding:encoding/base64"
    )
    assert base64_arm["requested_language"] == "am"


def test_every_reviewed_transform_and_all_registers_are_selectable_across_waves() -> None:
    seeds = (
        {
            "technique_id": "cat/direct/override-baseline",
            "semantic_id": "cat/direct/override-baseline",
            "objective": "Test the reviewed target policy boundary.",
            "execution_role": "direct",
            "tags": ("prompt-injection",),
            "language": "en",
        },
    )
    existing = ()
    selected: set[str] = set()
    for _wave in range(140):
        candidates = catalog_mutation_candidates(seeds, existing, maximum=2)
        if not candidates:
            break
        selected.update(str(item["strategy_id"]) for item in candidates)
        existing = (*existing, *candidates)

    assert len({item for item in selected if item.startswith("mutation:register:")}) == 54
    assert len({item for item in selected if item.startswith("mutation:encoding:")}) == 222
    model_mediated = tuple(
        item["execution_reachability"]
        for item in existing
        if not item["execution_reachability"]["locally_executable"]
    )
    assert len(model_mediated) == 54 + 181
    assert all(
        evidence["executor_contract"]["schema"] == "merlin.model-mediated-executor.v1"
        and evidence["executor_contract"]["instruction_digest"].startswith("sha256:")
        for evidence in model_mediated
    )


def test_unknown_preservation_capability_does_not_suppress_hidden_transforms() -> None:
    seeds = (
        {
            "technique_id": "cat/direct/override-baseline",
            "objective": "Test the reviewed target policy boundary.",
            "execution_role": "direct",
        },
    )
    blocked = {
        "mutation:encoding:concealment/invisible-text",
        "mutation:encoding:concealment/whitespace-steganography",
        "mutation:encoding:concealment/zerowidth-steganography",
    }
    full_frontier = catalog_mutation_candidates(seeds, (), maximum=1_000)
    visible_existing = tuple(
        item for item in full_frontier if str(item["strategy_id"]) not in blocked
    )
    candidates = catalog_mutation_candidates(seeds, visible_existing, maximum=6)
    assert blocked <= {str(item["strategy_id"]) for item in candidates}
    for candidate in candidates:
        if str(candidate["strategy_id"]) not in blocked:
            continue
        reachability = candidate["execution_reachability"]
        assert reachability["production_reachable"] is True
        assert reachability["reason_class"] == "transport-preservation-unverified"
        assert reachability["required_capabilities"] == ("unicode_tag_preservation",)
