from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
LAUNCHER = REPOSITORY / "packaging" / "merlin-launcher"


def _bundle(root: Path, *, version: str) -> Path:
    bundle = root / "merlin-0.1.0-darwin-arm64"
    runtime = bundle / "payload" / "merlin-runtime" / "merlin-runtime"
    postgres = bundle / "payload" / "postgresql" / "18.1.0"
    runtime.parent.mkdir(parents=True)
    postgres.mkdir(parents=True)
    shutil.copy2(LAUNCHER, bundle / "merlin")
    (bundle / "merlin").chmod(0o755)
    runtime.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'printf \'%s\\n\' "$*" >> "$MERLIN_TEST_LOG"\n'
        'case "${1:-}" in\n'
        f'  --version) echo "merlin {version}" ;;\n'
        "  daemon)\n"
        '    case "${2:-}" in\n'
        '      start) printf running > "$MERLIN_TEST_STATE"; echo "Merlin daemon ready" ;;\n'
        '      stop) rm -f "$MERLIN_TEST_STATE"; echo "Merlin daemon stopped" ;;\n'
        '      status) if [ -e "$MERLIN_TEST_STATE" ]; then echo running; '
        "else echo stopped; fi ;;\n"
        "    esac ;;\n"
        "  storage)\n"
        '    case "${2:-}" in purge) rm -rf "$HOME/.merlin" ;; esac ;;\n'
        '  "") mkdir -p "$HOME/.merlin"; printf configured > '
        '"$HOME/.merlin/config.toml" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    return bundle


def _run(
    binary: Path, *arguments: str, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_packaged_lifecycle_from_arbitrary_working_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "unrelated" / "working-directory"
    source_v1 = _bundle(tmp_path / "v1", version="0.1.0+first")
    source_v2 = _bundle(tmp_path / "v2", version="0.1.0+replacement")
    outside.mkdir(parents=True)
    home.mkdir()
    log = tmp_path / "runtime.log"
    state = tmp_path / "daemon.state"
    env = {
        **os.environ,
        "HOME": str(home),
        "MERLIN_TEST_LOG": str(log),
        "MERLIN_TEST_STATE": str(state),
    }

    first = _run(source_v1 / "merlin", cwd=outside, env=env)
    installed = home / ".local" / "bin" / "merlin"
    assert first.returncode == 0, first.stderr
    assert installed.is_symlink()
    assert (home / ".merlin" / "config.toml").read_text() == "configured"
    assert log.read_text().splitlines()[-1] == ""

    hidden_install = _run(installed, "install", cwd=outside, env=env)
    assert hidden_install.returncode == 2
    assert "installs automatically" in hidden_install.stderr

    assert _run(installed, "status", cwd=outside, env=env).stdout.strip() == "stopped"
    assert _run(installed, "start", cwd=outside, env=env).returncode == 0
    assert _run(installed, "status", cwd=outside, env=env).stdout.strip() == "running"
    assert _run(installed, "restart", cwd=outside, env=env).returncode == 0
    assert _run(installed, "stop", cwd=outside, env=env).returncode == 0

    update = _run(source_v2 / "merlin", "update", "--channel", "preview", cwd=outside, env=env)
    assert update.returncode == 0, update.stderr
    assert "merlin 0.1.0+replacement" in update.stdout
    assert "merlin 0.1.0+replacement" in _run(installed, "--version", cwd=outside, env=env).stdout
    assert (home / ".local" / "share" / "merlin" / "release-channel").read_text() == ("preview\n")
    assert not tuple((home / ".local" / "share" / "merlin").glob(".previous-*"))
    assert not tuple((home / ".local" / "share" / "merlin").glob(".install.*"))

    invalid_channel = _run(
        source_v2 / "merlin", "update", "--channel", "bad channel", cwd=outside, env=env
    )
    assert invalid_channel.returncode == 2
    assert (home / ".local" / "share" / "merlin" / "release-channel").read_text() == ("preview\n")
    assert "merlin 0.1.0+replacement" in _run(installed, "--version", cwd=outside, env=env).stdout

    (home / ".merlin" / "keep").write_text("state", encoding="utf-8")
    kept = _run(installed, "uninstall", "--keep-data", cwd=outside, env=env)
    assert kept.returncode == 0
    assert not installed.exists()
    assert (home / ".merlin" / "keep").is_file()

    reinstall = _run(source_v2 / "merlin", cwd=outside, env=env)
    assert reinstall.returncode == 0
    assert (home / ".local" / "share" / "merlin" / "release-channel").read_text() == ("preview\n")
    (home / ".merlin" / "purge-me").write_text("state", encoding="utf-8")
    purged = _run(installed, "uninstall", cwd=outside, env=env)
    assert purged.returncode == 0
    assert "all Merlin data purged" in purged.stdout
    assert not installed.exists()
    assert not (home / ".merlin").exists()
    assert not (home / ".local" / "share" / "merlin" / "release-channel").exists()
