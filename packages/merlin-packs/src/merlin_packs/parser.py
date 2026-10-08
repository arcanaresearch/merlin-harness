import tomllib
from pathlib import Path

from merlin_contracts import AttackCase, AttackPack
from pydantic import ValidationError

from .errors import PackFormatError


def _document(path: Path) -> tuple[dict[str, object], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PackFormatError(f"cannot read {path}: {error}") from error
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise PackFormatError(f"{path}: expected TOML front matter starting with +++")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "+++")
    except StopIteration as error:
        raise PackFormatError(f"{path}: TOML front matter has no closing +++") from error
    try:
        metadata = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as error:
        raise PackFormatError(f"{path}: invalid TOML front matter: {error}") from error
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise PackFormatError(f"{path}: Markdown instructions must not be empty")
    return metadata, body


def _validated(model_type, values: dict[str, object], path: Path):
    try:
        return model_type.model_validate(values)
    except ValidationError as error:
        raise PackFormatError(f"{path}: {error}") from error


def parse_manifest(path: Path) -> tuple[dict[str, object], list[str]]:
    """Parse PACK.md, returning validated pack fields and case paths."""
    metadata, instructions = _document(path)
    case_files = metadata.pop("cases", None)
    if (
        not isinstance(case_files, list)
        or not case_files
        or not all(isinstance(item, str) and item for item in case_files)
    ):
        raise PackFormatError(f"{path}: cases must be a non-empty array of paths")
    values = {**metadata, "instructions": instructions, "cases": []}
    pack = _validated(AttackPack, values, path)
    return pack.model_dump(exclude={"cases"}), case_files


def parse_case(path: Path) -> AttackCase:
    metadata, instructions = _document(path)
    return _validated(AttackCase, {**metadata, "instructions": instructions}, path)


def load_pack(directory: Path) -> AttackPack:
    """Load a pack directory without executing any content from it."""
    directory = directory.resolve()
    fields, case_files = parse_manifest(directory / "PACK.md")
    cases: list[AttackCase] = []
    seen: set[str] = set()
    for relative in case_files:
        case_path = (directory / relative).resolve()
        if not case_path.is_relative_to(directory):
            raise PackFormatError(f"{directory / 'PACK.md'}: case path escapes pack directory")
        case = parse_case(case_path)
        if case.id in seen:
            raise PackFormatError(f"{directory / 'PACK.md'}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    known = {case.id for case in cases}
    for case in cases:
        for relationship, references in (
            ("derived_from", case.derived_from),
            ("combines", case.combines),
        ):
            if len(references) != len(set(references)):
                raise PackFormatError(
                    f"{directory / 'PACK.md'}: {case.id!r} has duplicate {relationship} references"
                )
            if case.id in references:
                raise PackFormatError(
                    f"{directory / 'PACK.md'}: {case.id!r} cannot reference itself "
                    f"in {relationship}"
                )
            if missing := sorted(set(references) - known):
                raise PackFormatError(
                    f"{directory / 'PACK.md'}: {case.id!r} has unknown {relationship} "
                    f"references: {', '.join(missing)}"
                )
    return AttackPack(**fields, cases=cases)
