from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: hash-wheelhouse.py WHEELHOUSE OUTPUT")
    wheelhouse = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    repository = Path(__file__).resolve().parents[1]
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise SystemExit(f"no wheels found in {wheelhouse}")
    files = []
    for wheel in wheels:
        files.append(
            {
                "filename": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "size": wheel.stat().st_size,
            }
        )
    document = {
        "schema_version": 1,
        "build": {
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "dirty": bool(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            ),
        },
        "target": "macosx_14_0_arm64",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "machine": platform.machine(),
        "files": files,
    }
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
