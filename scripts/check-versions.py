#!/usr/bin/env python3
"""Fail a release when workspace package versions have drifted."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    manifests = (
        root / "pyproject.toml",
        *sorted(root.glob("apps/*/pyproject.toml")),
        *sorted(root.glob("packages/*/pyproject.toml")),
    )
    versions: dict[str, list[str]] = {}
    for manifest in manifests:
        with manifest.open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
        name, version = project.get("name"), project.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions.setdefault(version, []).append(f"{name} ({manifest.relative_to(root)})")
    if len(versions) == 1:
        version = next(iter(versions))
        print(f"Merlin workspace version {version} is consistent")
        return 0
    print("Merlin workspace versions disagree:", file=sys.stderr)
    for version, packages in sorted(versions.items()):
        print(f"  {version}: {', '.join(packages)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
