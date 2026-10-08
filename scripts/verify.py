"""Small, time-bounded verification entry point defined by PLAN.md."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHASE_PACKAGES = {
    1: (
        "packages/merlin-contracts",
        "packages/merlin-config",
        "packages/merlin-storage-postgres",
        "packages/merlin-providers",
        "packages/merlin-tools",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    2: (
        "packages/merlin-contracts",
        "packages/merlin-providers",
        "packages/merlin-packs",
        "packages/merlin-assessments",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    3: (
        "packages/merlin-contracts",
        "packages/merlin-skills",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    4: (
        "packages/merlin-contracts",
        "packages/merlin-mcp",
        "packages/merlin-tools",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    5: (
        "packages/merlin-contracts",
        "packages/merlin-agents",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    6: (
        "packages/merlin-contracts",
        "packages/merlin-findings",
        "packages/merlin-agents",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    7: (
        "packages/merlin-permissions",
        "packages/merlin-tools",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    8: (
        "packages/merlin-mcp",
        "packages/merlin-tools",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    9: (
        "packages/merlin-browser",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    10: (
        "packages/merlin-memory",
        "packages/merlin-agents",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    11: (
        "packages/merlin-contracts",
        "packages/merlin-promotion",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    12: (
        "packages/merlin-readiness",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
    14: (
        "packages/merlin-contracts",
        "packages/merlin-config",
        "packages/merlin-providers",
        "packages/merlin-permissions",
        "packages/merlin-tools",
        "packages/merlin-mcp",
        "packages/merlin-browser",
        "packages/merlin-skills",
        "packages/merlin-assessments",
        "packages/merlin-agents",
        "packages/merlin-findings",
        "packages/merlin-memory",
        "packages/merlin-storage-postgres",
        "packages/merlin-kernel",
        "apps/merlin-tui",
    ),
}


def existing(paths: tuple[str, ...]) -> list[str]:
    return [path for path in paths if (ROOT / path).exists()]


def run(command: list[str], timeout: float) -> None:
    remaining = timeout
    started = time.monotonic()
    subprocess.run(command, cwd=ROOT, check=True, timeout=remaining)
    elapsed = time.monotonic() - started
    if elapsed > timeout:
        raise TimeoutError(f"verification exceeded {timeout:.0f}s")


def verify(paths: list[str], timeout: float) -> None:
    if not paths:
        raise SystemExit("no matching package paths exist")
    started = time.monotonic()
    run(["uv", "run", "ruff", "format", "--check", *paths], timeout)
    remaining = timeout - (time.monotonic() - started)
    run(["uv", "run", "ruff", "check", *paths], remaining)
    remaining = timeout - (time.monotonic() - started)
    run(["uv", "run", "pytest", *paths], remaining)
    print(f"verified in {time.monotonic() - started:.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="scope", required=True)
    package = subparsers.add_parser("package")
    package.add_argument("path")
    phase = subparsers.add_parser("phase")
    phase.add_argument("number", type=int)
    args = parser.parse_args()

    if args.scope == "package":
        path = args.path
        candidate = ROOT / path
        if not candidate.exists():
            matches = list(ROOT.glob(f"packages/*{path}*")) + list(ROOT.glob(f"apps/*{path}*"))
            if len(matches) != 1:
                raise SystemExit(f"package not found or ambiguous: {path}")
            path = str(matches[0].relative_to(ROOT))
        verify([path], 15.0)
        return

    paths = existing(PHASE_PACKAGES.get(args.number, ()))
    if args.number not in PHASE_PACKAGES:
        raise SystemExit(f"phase {args.number} has no verification manifest yet")
    verify(paths, 90.0)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"verification timed out: {error.cmd}") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from error
