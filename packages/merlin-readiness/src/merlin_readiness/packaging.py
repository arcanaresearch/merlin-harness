from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PackagingAudit:
    package: str
    python_requirement: str
    script: str
    wheel_backend: str
    macos_arm_embedded_postgres: bool


def audit_packaging(pyproject: Path, lockfile: Path) -> PackagingAudit:
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = document.get("project", {})
    build = document.get("build-system", {})
    scripts = project.get("scripts", {})
    if not isinstance(project.get("name"), str):
        raise ValueError("project.name is required")
    requirement = project.get("requires-python")
    if not isinstance(requirement, str) or "3.13" not in requirement:
        raise ValueError("package must declare Python 3.13 support")
    script = scripts.get("merlin") if isinstance(scripts, dict) else None
    if not isinstance(script, str):
        raise ValueError("merlin console script is required")
    backend = build.get("build-backend")
    if not isinstance(backend, str):
        raise ValueError("wheel build backend is required")
    lock = lockfile.read_text(encoding="utf-8")
    macos_arm = "pg0_embedded" in lock and "macosx_14_0_arm64.whl" in lock
    if not macos_arm:
        raise ValueError("uv lock lacks the pg0 macOS 14 ARM wheel")
    return PackagingAudit(project["name"], requirement, script, backend, macos_arm)
