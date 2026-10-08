import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import DuplicateSkillError, SkillNotFoundError, SkillReferenceError
from .parser import Skill, load_skill

_MENTION = re.compile(r"(?<![\w$])\$([A-Za-z0-9][A-Za-z0-9_-]*)")
_IGNORED_DIRECTORIES = {".git", ".hg", ".svn", ".venv", "node_modules", "__pycache__"}


@dataclass(frozen=True, slots=True)
class SkillInfo:
    name: str
    description: str
    directory: Path
    enabled: bool
    version: str


def default_roots(repository: Path | None = None) -> tuple[Path, ...]:
    repository = Path.cwd() if repository is None else Path(repository)
    home = Path.home()
    return repository, home / ".merlin" / "skills", home / ".agents" / "skills"


class SkillCatalog:
    """Discovers inert skill documents and tracks session-local enablement."""

    def __init__(self, roots: Iterable[Path] | None = None, *, repository: Path | None = None):
        self._roots = tuple(default_roots(repository) if roots is None else map(Path, roots))
        self._skills: dict[str, Skill] = {}
        self._locations: dict[str, Path] = {}
        self._enabled: set[str] = set()
        self.refresh()

    def refresh(self) -> None:
        skills: dict[str, Skill] = {}
        locations: dict[str, Path] = {}
        source_roots: dict[str, int] = {}
        for root_index, root in enumerate(self._roots):
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            for path in sorted(root.rglob("SKILL.md")):
                if any(part in _IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
                    continue
                if not path.resolve().is_relative_to(resolved_root):
                    continue
                skill = load_skill(path)
                if skill.name in skills and skills[skill.name].content == skill.content:
                    continue
                if skill.name in skills:
                    # Roots are ordered by trust/specificity: repository,
                    # Merlin-managed user skills, then the shared agent inventory.
                    # A lower-precedence copy must not brick daemon startup merely
                    # because it is older; the selected higher-precedence skill is
                    # still version-bound by SessionSkillService. Conflicting
                    # definitions within one root remain an explicit error.
                    if source_roots[skill.name] < root_index:
                        continue
                    raise DuplicateSkillError(
                        f"incompatible skill collision {skill.name!r}: "
                        f"{locations[skill.name]} ({skills[skill.name].version}) and "
                        f"{path} ({skill.version})"
                    )
                skills[skill.name] = skill
                locations[skill.name] = path
                source_roots[skill.name] = root_index
        self._skills = skills
        self._locations = locations
        self._enabled.intersection_update(skills)

    def list(self) -> list[SkillInfo]:
        return [self.inspect(name) for name in sorted(self._skills)]

    def inspect(self, name: str) -> SkillInfo:
        skill = self._get(name)
        return SkillInfo(
            name=skill.name,
            description=skill.description,
            directory=skill.directory,
            enabled=name in self._enabled,
            version=skill.version,
        )

    def load(self, name: str) -> Skill:
        return self._get(name)

    def load_reference(self, name: str, relative_path: str | Path) -> str:
        """Explicitly read one inert reference file contained by a skill bundle."""
        skill = self._get(name)
        reference = (skill.directory / relative_path).resolve()
        if not reference.is_relative_to(skill.directory) or reference == skill.directory:
            raise SkillReferenceError(f"reference path escapes skill {name!r}")
        if not reference.is_file():
            raise SkillReferenceError(f"reference does not exist: {relative_path}")
        try:
            return reference.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SkillReferenceError(f"cannot read reference {relative_path}: {error}") from error

    def enable(self, name: str) -> None:
        self._get(name)
        self._enabled.add(name)

    def disable(self, name: str) -> None:
        self._get(name)
        self._enabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        self._get(name)
        return name in self._enabled

    @property
    def enabled_ids(self) -> frozenset[str]:
        return frozenset(self._enabled)

    @staticmethod
    def extract_mentions(text: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_MENTION.findall(text)))

    def resolve_mentions(self, text: str) -> tuple[Skill, ...]:
        return tuple(self._get(name) for name in self.extract_mentions(text))

    def _get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise SkillNotFoundError(f"unknown skill {name!r}") from error
