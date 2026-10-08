from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "release_contract.py"


def test_release_identity_matches_manifest_binary_folder_archive_and_channel(
    tmp_path: Path,
) -> None:
    name = "merlin-1.2.3-darwin-arm64"
    release = tmp_path / name
    release.mkdir()
    manifest = release / "manifest.json"
    manifest.write_text(json.dumps({"files": []}), encoding="utf-8")
    (release / "release-channel").write_text("preview\n", encoding="utf-8")
    binary = release / "merlin"
    binary.write_text("#!/bin/sh\necho 'merlin 1.2.3'\n", encoding="utf-8")
    binary.chmod(0o755)
    archive = tmp_path / f"{name}.tar.gz"

    common = [
        "--manifest",
        str(manifest),
        "--version",
        "1.2.3",
        "--name",
        name,
        "--platform",
        "darwin-arm64",
        "--channel",
        "preview",
    ]
    subprocess.run([sys.executable, str(SCRIPT), "stamp", *common], check=True)
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(release, arcname=name)
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "verify",
            *common,
            "--binary",
            str(binary),
            "--archive",
            str(archive),
        ],
        check=True,
    )

    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["release"]["version"] = "1.2.2"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", *common], text=True, capture_output=True
    )
    assert rejected.returncode != 0
    assert "manifest mismatch" in rejected.stderr
