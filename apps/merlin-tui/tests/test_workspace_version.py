from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY / "scripts" / "set_workspace_version.py"


def test_workspace_version_progression_updates_manifests_and_lock(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    for source in (
        REPOSITORY / "pyproject.toml",
        *sorted(REPOSITORY.glob("apps/*/pyproject.toml")),
        *sorted(REPOSITORY.glob("packages/*/pyproject.toml")),
    ):
        destination = root / source.relative_to(REPOSITORY)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(REPOSITORY / "uv.lock", root / "uv.lock")

    current = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())["project"]["version"]
    major, minor, patch = (int(part) for part in current.split("."))
    next_version = f"{major}.{minor}.{patch + 1}"

    advanced = subprocess.run(
        [sys.executable, str(SCRIPT), next_version, "--root", str(root)],
        text=True,
        capture_output=True,
    )
    assert advanced.returncode == 0, advanced.stderr
    manifests = (
        root / "pyproject.toml",
        *root.glob("apps/*/pyproject.toml"),
        *root.glob("packages/*/pyproject.toml"),
    )
    assert {tomllib.loads(path.read_text())["project"]["version"] for path in manifests} == {
        next_version
    }
    assert (root / "uv.lock").read_text().count(f'version = "{next_version}"') >= len(manifests)

    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), current, "--root", str(root)],
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert "must be greater" in rejected.stderr
