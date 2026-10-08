#!/usr/bin/env python3
"""Advance every Merlin workspace package and lock entry as one checked operation."""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version


def set_version(root: Path, requested: str, *, dry_run: bool = False) -> tuple[str, int]:
    try:
        target = Version(requested)
    except InvalidVersion as error:
        raise SystemExit(f"invalid version: {requested!r}") from error
    manifests = (
        root / "pyproject.toml",
        *sorted(root.glob("apps/*/pyproject.toml")),
        *sorted(root.glob("packages/*/pyproject.toml")),
    )
    current: dict[str, str] = {}
    names: set[str] = set()
    originals: dict[Path, str] = {}
    replacements: dict[Path, str] = {}
    for path in manifests:
        text = path.read_text(encoding="utf-8")
        project = tomllib.loads(text)["project"]
        name, version = str(project["name"]), str(project["version"])
        names.add(name)
        current[name] = version
        originals[path] = text
        replacements[path] = re.sub(
            r'(?m)^version = "[^"]+"$', f'version = "{target}"', text, count=1
        )
    observed = set(current.values())
    if len(observed) != 1:
        raise SystemExit(f"workspace versions already disagree: {sorted(observed)!r}")
    previous = Version(observed.pop())
    if target <= previous:
        raise SystemExit(f"new version {target} must be greater than current version {previous}")

    lock = root / "uv.lock"
    lock_text = lock.read_text(encoding="utf-8")
    updated_lock = lock_text
    for name in sorted(names):
        pattern = re.compile(rf'(?m)(^name = "{re.escape(name)}"\nversion = ")[^"]+("$)')
        updated_lock, count = pattern.subn(rf"\g<1>{target}\2", updated_lock, count=1)
        if count != 1:
            raise SystemExit(f"workspace package {name!r} is missing from uv.lock")
    originals[lock] = lock_text
    replacements[lock] = updated_lock
    if not dry_run:
        _replace_all(originals, replacements)
    return str(target), len(manifests)


def _replace_all(originals: dict[Path, str], replacements: dict[Path, str]) -> None:
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for path, text in replacements.items():
            temporary = path.with_name(f".{path.name}.version-{os.getpid()}")
            temporary.write_text(text, encoding="utf-8")
            staged[path] = temporary
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except BaseException:
        for path in replaced:
            path.write_text(originals[path], encoding="utf-8")
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    version, count = set_version(args.root.resolve(), args.version, dry_run=args.dry_run)
    verb = "would update" if args.dry_run else "updated"
    print(f"{verb} {count} workspace packages and uv.lock to {version}")


if __name__ == "__main__":
    main()
