import asyncio
from pathlib import Path

import pytest
from merlin_skills import (
    DuplicateSkillError,
    SessionSkillService,
    SkillCatalog,
    SkillFormatError,
    SkillNotFoundError,
    SkillReferenceError,
    load_skill,
)


def _write_skill(root: Path, name: str = "demo") -> Path:
    directory = root / "nested" / name
    (directory / "references").mkdir(parents=True)
    document = directory / "SKILL.md"
    document.write_text(
        f"---\nname: {name}\ndescription: Demo instructions\n---\n\n# {name}\n\nDo the work.\n",
        encoding="utf-8",
    )
    (directory / "references" / "guide.md").write_text("safe reference", encoding="utf-8")
    (directory / "script.sh").write_text("exit 99", encoding="utf-8")
    return document


def test_load_preserves_complete_document(tmp_path: Path) -> None:
    path = _write_skill(tmp_path)
    skill = load_skill(path)
    assert skill.name == "demo"
    assert skill.description == "Demo instructions"
    assert skill.content == path.read_text(encoding="utf-8")


def test_catalog_discovers_recursively_and_tracks_enablement(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    catalog = SkillCatalog([tmp_path])
    assert [item.name for item in catalog.list()] == ["demo"]
    assert not catalog.inspect("demo").enabled
    catalog.enable("demo")
    assert catalog.is_enabled("demo")
    assert catalog.enabled_ids == frozenset({"demo"})
    catalog.disable("demo")
    assert not catalog.inspect("demo").enabled


def test_mentions_are_unique_ordered_and_resolved(tmp_path: Path) -> None:
    first = _write_skill(tmp_path, "first-skill")
    second = _write_skill(tmp_path, "second_skill")
    catalog = SkillCatalog([tmp_path])
    assert catalog.extract_mentions("$first-skill then $second_skill and $first-skill") == (
        "first-skill",
        "second_skill",
    )
    assert [skill.content for skill in catalog.resolve_mentions("use $second_skill")] == [
        second.read_text(encoding="utf-8")
    ]
    assert first.is_file()


def test_references_require_explicit_contained_path(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    catalog = SkillCatalog([tmp_path])
    assert catalog.load_reference("demo", "references/guide.md") == "safe reference"
    with pytest.raises(SkillReferenceError, match="escapes"):
        catalog.load_reference("demo", "../../../outside.md")


def test_unknown_and_malformed_skills_have_domain_errors(tmp_path: Path) -> None:
    catalog = SkillCatalog([tmp_path])
    with pytest.raises(SkillNotFoundError, match="missing"):
        catalog.load("missing")
    broken = tmp_path / "broken" / "SKILL.md"
    broken.parent.mkdir()
    broken.write_text("# no front matter", encoding="utf-8")
    with pytest.raises(SkillFormatError, match="front matter"):
        SkillCatalog([tmp_path])


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    _write_skill(tmp_path / "one")
    second = _write_skill(tmp_path / "two")
    second.write_text(second.read_text(encoding="utf-8") + "different\n", encoding="utf-8")
    with pytest.raises(DuplicateSkillError, match="collision"):
        SkillCatalog([tmp_path])


def test_discovery_does_not_follow_file_symlink_outside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    document = _write_skill(outside)
    root = tmp_path / "root"
    linked = root / "linked" / "SKILL.md"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(document)
    assert SkillCatalog([root]).list() == []


def test_identical_duplicate_is_deduplicated_but_changed_body_collides(tmp_path: Path) -> None:
    first = _write_skill(tmp_path / "one")
    second = _write_skill(tmp_path / "two")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    assert [item.name for item in SkillCatalog([tmp_path]).list()] == ["demo"]
    second.write_text(second.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(DuplicateSkillError, match="incompatible skill collision"):
        SkillCatalog([tmp_path])


def test_higher_precedence_root_shadows_incompatible_shared_copy(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    shared = tmp_path / "shared"
    selected = _write_skill(repository)
    fallback = _write_skill(shared)
    selected.write_text(selected.read_text() + "repository guidance\n", encoding="utf-8")
    fallback.write_text(fallback.read_text() + "shared guidance\n", encoding="utf-8")

    catalog = SkillCatalog([repository, shared])

    assert catalog.load("demo").content.endswith("repository guidance\n")


def test_session_enablement_is_version_bound_and_persistent(tmp_path: Path) -> None:
    document = _write_skill(tmp_path / "root")
    document.write_text(
        document.read_text(encoding="utf-8").replace("description:", "version: '1'\ndescription:"),
        encoding="utf-8",
    )
    state = tmp_path / "state" / "skills.json"
    service = SessionSkillService(SkillCatalog([tmp_path / "root"]), state)
    result = asyncio.run(service.enable("session", "demo"))
    assert result["version"] == "1"
    restored = SessionSkillService(SkillCatalog([tmp_path / "root"]), state)
    assert len(asyncio.run(restored.instructions_for("session", ""))) == 1
    document.write_text(document.read_text().replace("version: '1'", "version: '2'"))
    with pytest.raises(ValueError, match="changed"):
        SessionSkillService(SkillCatalog([tmp_path / "root"]), state)
