#!/usr/bin/env python3
"""Increment the consistent workspace version and update every package manifest."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path


def next_version(version: str, part: str) -> str:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
    if match is None:
        raise ValueError(f"workspace version must be X.Y.Z, got {version!r}")
    major, minor, patch = (int(value) for value in match.groups())
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "major":
        major, minor, patch = major + 1, 0, 0
    else:
        raise ValueError("version part must be patch, minor, or major")
    return f"{major}.{minor}.{patch}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump-workspace-version.py patch|minor|major", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as handle:
        current = tomllib.load(handle)["project"]["version"]
    updated = next_version(current, sys.argv[1])
    subprocess.run(
        [sys.executable, str(root / "scripts/set_workspace_version.py"), updated],
        cwd=root,
        check=True,
    )
    print(f"Merlin workspace version bumped: {current} -> {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
