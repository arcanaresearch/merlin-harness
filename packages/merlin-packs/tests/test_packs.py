import json
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from merlin_contracts import AttackPack, ToolContext
from merlin_packs import (
    KnowledgeSearchTool,
    PackCatalog,
    PackFormatError,
    PackNotFoundError,
    knowledge_catalogue,
    load_knowledge_packs,
    load_pack,
    select_knowledge,
)
from merlin_packs._bundle_crypto import decrypt_builtin_pack


def _write_pack(root: Path, pack_id: str = "demo") -> Path:
    directory = root / pack_id
    (directory / "cases").mkdir(parents=True)
    (directory / "PACK.md").write_text(
        f'''+++
id = "{pack_id}"
version = "1.0"
title = "Demo"
target_kind = "ai-chat"
cases = ["cases/one.md"]
objectives = ["Test one thing"]
tags = ["demo"]
turn_limit = 3
+++
# Instructions

Exercise the target.
''',
        encoding="utf-8",
    )
    (directory / "cases" / "one.md").write_text(
        """+++
id = "one"
title = "First case"
objectives = ["Observe behavior"]
tags = ["demo"]
turn_limit = 2
success_hints = ["The behavior is observed"]
+++
Try one bounded interaction.
""",
        encoding="utf-8",
    )
    return directory


def test_loads_declarative_pack(tmp_path: Path) -> None:
    pack = load_pack(_write_pack(tmp_path))
    assert isinstance(pack, AttackPack)
    assert pack.id == "demo"
    assert pack.instructions.startswith("# Instructions")
    assert pack.cases[0].id == "one"


def test_pack_cases_expose_only_valid_explicit_relationships(tmp_path: Path) -> None:
    directory = _write_pack(tmp_path)
    manifest = directory / "PACK.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'cases = ["cases/one.md"]',
            'cases = ["cases/one.md", "cases/two.md"]',
        ),
        encoding="utf-8",
    )
    (directory / "cases" / "two.md").write_text(
        """+++
id = "two"
title = "Related case"
derived_from = ["one"]
combines = ["one"]
+++
Exercise an explicitly related technique.
""",
        encoding="utf-8",
    )

    pack = load_pack(directory)

    assert pack.cases[1].derived_from == ["one"]
    assert pack.cases[1].combines == ["one"]


def test_pack_rejects_unattributable_relationship_reference(tmp_path: Path) -> None:
    directory = _write_pack(tmp_path)
    case = directory / "cases" / "one.md"
    case.write_text(
        case.read_text(encoding="utf-8").replace(
            'title = "First case"', 'title = "First case"\nderived_from = ["missing"]'
        ),
        encoding="utf-8",
    )

    with pytest.raises(PackFormatError, match="unknown derived_from.*missing"):
        load_pack(directory)


def test_catalog_discovers_and_tracks_enablement(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    catalog = PackCatalog([tmp_path], include_builtin=False)
    assert [item.id for item in catalog.list()] == ["demo"]
    assert not catalog.inspect("demo").enabled
    catalog.enable("demo")
    assert catalog.inspect("demo").enabled
    assert catalog.enabled_ids == frozenset({"demo"})
    catalog.disable("demo")
    assert not catalog.is_enabled("demo")


def test_rust_injection_catalogue_is_bundled_and_reconciled() -> None:
    catalog = PackCatalog()
    pack = catalog.load("merlin-injection-taxonomy")
    assert len(pack.cases) == 83
    assert len({case.id for case in pack.cases}) == 83
    assert any("benign-control" in case.tags for case in pack.cases)
    assert "Declared taxonomy:" in pack.instructions
    assert "merlin-domain-catalog" in pack.tags
    hydra = next(
        case for case in pack.cases if case.id == "cat/multi-turn/hydra-catalogue-diversity-sample"
    )
    assert hydra.strategy_id == "hydra-select"
    assert hydra.control_case_id == "cat/multi-turn/benign-multiturn-clarify"
    additions = {
        case.id: case
        for case in pack.cases
        if case.id
        in {
            "cat/direct/contradiction-laundering",
            "cat/direct/multilingual-pivot",
            "cat/direct/symbolic-compression",
            "cat/multi-turn/partial-compliance-expansion",
            "cat/multi-turn/urgency-exception-pressure",
        }
    }
    assert len(additions) == 5
    assert "low-resource-language" in additions["cat/direct/multilingual-pivot"].tags
    assert "homoglyph-substitution" in additions["cat/direct/symbolic-compression"].tags
    assert additions["cat/multi-turn/partial-compliance-expansion"].strategy_id == "crescendo"
    assert all(case.control_case_id and case.oracle_id for case in additions.values())
    language_variants = {
        case.id: case for case in pack.cases if case.id.startswith("cat/direct/multilingual-pivot-")
    }
    assert set(language_variants) == {
        "cat/direct/multilingual-pivot-ms",
        "cat/direct/multilingual-pivot-zh-en",
    }
    assert "ms" in language_variants["cat/direct/multilingual-pivot-ms"].tags
    assert "zh-en-mixed" in language_variants["cat/direct/multilingual-pivot-zh-en"].tags
    assert "language:ms" in language_variants["cat/direct/multilingual-pivot-ms"].tags
    assert "language-variant:native" in language_variants["cat/direct/multilingual-pivot-ms"].tags
    assert "language:zh-en" in language_variants["cat/direct/multilingual-pivot-zh-en"].tags
    assert "language-variant:mixed" in language_variants["cat/direct/multilingual-pivot-zh-en"].tags


def test_eight_canonical_rpk_assets_are_authenticated_and_routable() -> None:
    packs = load_knowledge_packs()
    assert [pack.kind for pack in packs] == [
        "attack_catalogue",
        "benign_controls",
        "compliance_controls",
        "emerging_techniques",
        "industry_profiles",
        "interaction_guidance",
        "pricing_schedules",
        "target_capability_guidance",
    ]
    assert len(knowledge_catalogue()) == 8
    selected = select_knowledge("recon browser chatbot financial payments")
    assert selected
    assert len(selected) <= 12
    assert any("browser" in entry.routing_terms for entry in selected)


def test_compliance_controls_require_reviewed_bindings_and_independent_evidence() -> None:
    selected = select_knowledge(
        "test sales price discount binding contract confidentiality canary cross tenant privacy",
        limit=12,
    )
    controls = {
        item.entry_id: json.loads(item.content)
        for item in selected
        if item.pack_id == "com.merlin.compliance-controls"
    }

    assert set(controls) == {
        "compliance.business.commitment-authority",
        "compliance.business.price-authority",
        "compliance.confidentiality.synthetic-canary",
        "compliance.privacy.subject-isolation",
    }
    assert all(item["binding_required"] for item in controls.values())
    assert all(item["benign_controls"] for item in controls.values())
    assert all(item["evidence_requirements"] for item in controls.values())
    assert all(item["inconclusive_when"] for item in controls.values())
    assert all(item["absence_authority"] == "bounded_only" for item in controls.values())
    assert controls["compliance.confidentiality.synthetic-canary"]["test_data_class"] == (
        "synthetic_only"
    )
    assert controls["compliance.privacy.subject-isolation"]["test_data_class"] == ("synthetic_only")


def test_resource_exhaustion_frontier_is_explicit_and_safely_bounded() -> None:
    selected = select_knowledge(
        "Poison Soup derailment token bomb output spam context exhaustion denial of wallet",
        limit=12,
    )
    entry = next(item for item in selected if item.entry_id == "emerging.poison-soup-token-waste")
    guidance = json.loads(entry.content)

    assert guidance["family"] == "derailment / Poison Soup and token bomb"
    assert {
        "input token spam",
        "output token spam",
        "recursive expansion or amplification",
        "context-window exhaustion",
        "latency amplification",
        "denial-of-wallet or cost amplification",
        "truncation and recovery behavior",
    } <= set(guidance["variants"])
    assert guidance["safety_bounds"] == {
        "abort_on_rate_limit_or_cost_warning": True,
        "abort_on_target_degradation": True,
        "maximum_amplification_ratio": 4,
        "maximum_attempts_per_variant": 2,
        "maximum_requested_output_tokens": 4096,
        "require_operator_budget": True,
    }
    assert "never infer cost" in guidance["reporting"]

    pack = PackCatalog().load("merlin-injection-taxonomy")
    experiment_ids = {
        "cat/multi-turn/crescendo-context-drift",
        "cat/multi-turn/poison-soup-token-waste",
        "cat/multi-turn/input-token-amplification",
        "cat/multi-turn/output-token-amplification",
        "cat/multi-turn/recursive-expansion",
        "cat/multi-turn/context-window-exhaustion",
        "cat/multi-turn/latency-amplification",
        "cat/multi-turn/denial-of-wallet",
        "cat/multi-turn/truncation-recovery",
    }
    experiments = {case.id: case for case in pack.cases if case.id in experiment_ids}

    assert set(experiments) == experiment_ids
    assert len({case.tags[-1] for case in experiments.values()}) > 1
    for case in experiments.values():
        assert case.execution_role == "direct"
        assert case.control_case_id == "cat/multi-turn/benign-multiturn-clarify"
        assert case.oracle_id == "oracle/host-side-reobservation"
        assert "executable-experiment" in case.tags
        assert case.turn_limit >= 2
        assert "control_procedure" in case.instructions
        assert "attack_procedure" in case.instructions
        assert "measurements" in case.instructions
        assert "recovery_check" in case.instructions
        assert "stop_conditions" in case.instructions
        assert "activity alone is not a result" in case.instructions
        assert case.success_hints[-1].endswith("cancelled, or ambiguous")


def test_builtin_knowledge_assets_are_encrypted_and_authenticated() -> None:
    root = Path(__file__).parents[1] / "src/merlin_packs/knowledge"
    encrypted_assets = sorted(root.glob("*.rpk.enc"))
    assert len(encrypted_assets) == 8
    for encrypted in encrypted_assets:
        payload = encrypted.read_bytes()
        assert payload.startswith(b"MERLIN-RPK-AESGCM-V1\n")
        decrypt_builtin_pack(encrypted.name.removesuffix(".enc"), payload)

    payload = encrypted_assets[0].read_bytes()
    damaged = bytearray(payload)
    damaged[-1] ^= 1
    with pytest.raises(InvalidTag):
        decrypt_builtin_pack(encrypted_assets[0].name.removesuffix(".enc"), bytes(damaged))


def test_release_packaging_accepts_only_complete_encrypted_pack_set(tmp_path: Path) -> None:
    repository = Path(__file__).parents[3]
    script_path = repository / "scripts/verify_encrypted_packs.py"
    spec = spec_from_file_location("verify_encrypted_packs", script_path)
    assert spec and spec.loader
    verifier = module_from_spec(spec)
    spec.loader.exec_module(verifier)

    source = Path(__file__).parents[1] / "src/merlin_packs/knowledge"
    collector = tmp_path / "collector/merlin_packs/knowledge"
    collector.mkdir(parents=True)
    wheel = tmp_path / "merlin_packs.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for encrypted in source.glob("*.rpk.enc"):
            payload = encrypted.read_bytes()
            (collector / encrypted.name).write_bytes(payload)
            archive.writestr(f"merlin_packs/knowledge/{encrypted.name}", payload)

    verifier.verify_wheel(wheel)
    verifier.verify_collector(tmp_path / "collector")

    (collector / "attack-catalogue.rpk").write_text("plaintext", encoding="utf-8")
    with pytest.raises(ValueError, match="plaintext knowledge packs"):
        verifier.verify_collector(tmp_path / "collector")


@pytest.mark.asyncio
async def test_knowledge_search_is_bounded_and_repeatable() -> None:
    tool = KnowledgeSearchTool()
    context = ToolContext(session_id="session-1", workspace_root=".")

    browser = await tool.invoke(context, {"query": "browser chatbot", "limit": 2})
    pricing = await tool.invoke(context, {"query": "pricing tokens", "limit": 2})

    assert browser.metadata["returned"] <= 2
    assert pricing.metadata["returned"] <= 2
    assert browser.content != pricing.content


def test_unknown_pack_has_domain_error(tmp_path: Path) -> None:
    catalog = PackCatalog([tmp_path], include_builtin=False)
    with pytest.raises(PackNotFoundError, match="missing"):
        catalog.load("missing")


def test_rejects_case_path_escape(tmp_path: Path) -> None:
    directory = _write_pack(tmp_path)
    manifest = directory / "PACK.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'cases = ["cases/one.md"]', 'cases = ["../outside.md"]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(PackFormatError, match="escapes"):
        load_pack(directory)
