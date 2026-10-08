from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]


def _installer_fixture(tmp_path: Path, *, stop_exit: int) -> tuple[Path, dict[str, str], Path]:
    repository = tmp_path / "repository"
    script = repository / "scripts" / "install-release-macos-arm.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY / "scripts" / script.name, script)

    python = repository / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho 0.1.0\n", encoding="utf-8")
    python.chmod(0o755)

    release_name = "merlin-0.1.0-darwin-arm64"
    payload = tmp_path / "payload" / release_name
    payload.mkdir(parents=True)
    released_binary = payload / "merlin"
    released_binary.write_text("#!/bin/sh\necho merlin 0.1.0\n", encoding="utf-8")
    released_binary.chmod(0o755)
    archive = repository / "target" / "releases" / f"{release_name}.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname=release_name)

    install_root = tmp_path / "install"
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    log = tmp_path / "stop.log"
    installed = bin_root / "merlin"
    installed.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "$MERLIN_TEST_STOP_LOG"\nexit {stop_exit}\n',
        encoding="utf-8",
    )
    installed.chmod(0o755)
    old_release = install_root / release_name
    old_release.mkdir(parents=True)
    (old_release / "marker").write_text("old", encoding="utf-8")
    env = {
        **os.environ,
        "MERLIN_INSTALL_ROOT": str(install_root),
        "MERLIN_BIN_ROOT": str(bin_root),
        "MERLIN_TEST_STOP_LOG": str(log),
    }
    return script, env, old_release


def test_install_release_stops_daemon_before_replacing_bundle(tmp_path: Path) -> None:
    script, env, old_release = _installer_fixture(tmp_path, stop_exit=0)
    result = subprocess.run([str(script)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stop.log").read_text(encoding="utf-8") == "stop\n"
    assert not (old_release / "marker").exists()
    assert "merlin 0.1.0" in (old_release / "merlin").read_text(encoding="utf-8")


def test_install_release_preserves_bundle_when_daemon_stop_fails(tmp_path: Path) -> None:
    script, env, old_release = _installer_fixture(tmp_path, stop_exit=1)
    result = subprocess.run([str(script)], env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert (tmp_path / "stop.log").read_text(encoding="utf-8") == "stop\n"
    assert (old_release / "marker").read_text(encoding="utf-8") == "old"
