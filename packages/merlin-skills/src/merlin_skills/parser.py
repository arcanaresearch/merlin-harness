from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import SkillFormatError


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    content: str
    directory: Path
    version: str


def load_skill(path: Path) -> Skill:
    """Parse metadata while retaining the complete SKILL.md as provider context."""
    path = Path(path).resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SkillFormatError(f"cannot read {path}: {error}") from error

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillFormatError(f"{path}: expected YAML front matter starting with ---")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as error:
        raise SkillFormatError(f"{path}: YAML front matter has no closing ---") from error

    try:
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as error:
        raise SkillFormatError(f"{path}: invalid YAML front matter: {error}") from error
    if not isinstance(metadata, dict):
        raise SkillFormatError(f"{path}: YAML front matter must be a mapping")

    raw_name = metadata.get("name", "")
    raw_description = metadata.get("description", "")
    raw_version = metadata.get("version", "unversioned")
    if not all(isinstance(value, str) for value in (raw_name, raw_description, raw_version)):
        raise SkillFormatError(f"{path}: name, description, and version must be strings")
    name = raw_name.strip()
    description = raw_description.strip()
    version = raw_version.strip()
    if not name:
        raise SkillFormatError(f"{path}: name must be a non-empty string")
    if not description:
        raise SkillFormatError(f"{path}: description must be a non-empty string")
    if not version:
        raise SkillFormatError(f"{path}: version must be a non-empty string")
    if any(character.isspace() for character in name) or "$" in name:
        raise SkillFormatError(f"{path}: name must not contain whitespace or '$'")
    return Skill(
        name=name, description=description, content=content, directory=path.parent, version=version
    )
