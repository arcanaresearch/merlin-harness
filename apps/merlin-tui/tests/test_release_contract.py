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


def test_release_contract_rejects_absolute_symlinks_in_bundle_and_archive(
    tmp_path: Path,
) -> None:
    name = "merlin-1.2.3-darwin-arm64"
    release = tmp_path / name
    release.mkdir()
    manifest = release / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "release": {
                    "archive": f"{name}.tar.gz",
                    "channel": "stable",
                    "name": name,
                    "platform": "darwin-arm64",
                    "version": "1.2.3",
                }
            }
        ),
        encoding="utf-8",
    )
    (release / "release-channel").write_text("stable\n", encoding="utf-8")
    absolute = release / "absolute"
    absolute.symlink_to("/private/tmp/legacy-release")
    dylib = release / "libpq.5.dylib"
    dylib.write_text("fixture", encoding="utf-8")
    (release / "libpq.dylib").symlink_to(dylib.name)
    inside = release / "inside"
    inside.write_text("fixture", encoding="utf-8")
    sub = release / "sub"
    sub.mkdir()
    (sub / "link").symlink_to("../inside")
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
        "stable",
    ]

    bundle_rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", *common], text=True, capture_output=True
    )
    assert bundle_rejected.returncode != 0
    assert "bundle contains absolute symlink" in bundle_rejected.stderr

    absolute.unlink()
    escape = release / "escape"
    escape.symlink_to("../outside")
    bundle_escape_rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", *common], text=True, capture_output=True
    )
    assert bundle_escape_rejected.returncode != 0
    assert "bundle symlink escapes bundle" in bundle_escape_rejected.stderr
    escape.unlink()
    subprocess.run([sys.executable, str(SCRIPT), "verify", *common], check=True)

    absolute.symlink_to("/private/tmp/legacy-release")
    archive = tmp_path / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz", dereference=False) as bundle:
        bundle.add(release, arcname=name)
    absolute.unlink()
    archive_rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", *common, "--archive", str(archive)],
        text=True,
        capture_output=True,
    )
    assert archive_rejected.returncode != 0
    assert "archive contains absolute link" in archive_rejected.stderr

    escape = release / "escape"
    escape.symlink_to("../outside")
    with tarfile.open(archive, "w:gz", dereference=False) as bundle:
        bundle.add(release, arcname=name)
    escape.unlink()
    escape_rejected = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", *common, "--archive", str(archive)],
        text=True,
        capture_output=True,
    )
    assert escape_rejected.returncode != 0
    assert "archive link escapes bundle" in escape_rejected.stderr

    for unsafe_name in (
        f"{name}/../outside",
        f"{name}/sub/../../outside",
        f"{name}/./inside",
        f"{name}/sub/../inside",
    ):
        with tarfile.open(archive, "w:gz", dereference=False) as bundle:
            bundle.add(release, arcname=name)
            bundle.addfile(tarfile.TarInfo(unsafe_name))
        member_rejected = subprocess.run(
            [sys.executable, str(SCRIPT), "verify", *common, "--archive", str(archive)],
            text=True,
            capture_output=True,
        )
        assert member_rejected.returncode != 0
        assert "archive contains unsafe path" in member_rejected.stderr


def test_release_build_writes_portable_checksum_and_removes_only_legacy_postgres_link() -> None:
    build = SCRIPT.with_name("build-release-macos-arm.sh").read_text(encoding="utf-8")

    assert 'legacy_postgres_link="$release_root/payload/postgresql/$postgres_version/' in build
    assert '"$HOME"/.local/share/redpen/redpen-*-darwin-arm64/payload/postgresql/' in build
    assert 'rm -f -- "$legacy_postgres_link"' in build
    assert 'echo "unexpected self-named PostgreSQL link:' in build
    assert 'elif [ -e "$legacy_postgres_link" ]; then' in build
    assert 'echo "unexpected self-named PostgreSQL entry:' in build
    assert 'find "$release_root/payload/postgresql' not in build
    assert 'shasum -a 256 "$(basename "$archive")"' in build
    assert 'shasum -a 256 "$archive"' not in build


def test_launcher_install_preserves_real_postgres_directory_and_links_missing_source(
    tmp_path: Path,
) -> None:
    release = tmp_path / "merlin-1.2.3-darwin-arm64"
    release.mkdir()
    launcher_source = Path(__file__).resolve().parents[3] / "packaging" / "merlin-launcher"
    launcher = release / "merlin"
    launcher.write_bytes(launcher_source.read_bytes())
    launcher.chmod(0o755)
    runtime = release / "payload" / "merlin-runtime" / "merlin-runtime"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nprintf 'merlin 1.2.3\\n'\n", encoding="utf-8")
    runtime.chmod(0o755)
    bundled_postgres = release / "payload" / "postgresql" / "18.1.0"
    bundled_postgres.mkdir(parents=True)
    (release / "release-channel").write_text("stable\n", encoding="utf-8")

    for source_exists in (True, False):
        home = tmp_path / ("home-existing" if source_exists else "home-missing")
        postgres_source = home / ".pg0" / "installation" / "18.1.0"
        marker = postgres_source / "postgresql.conf"
        if source_exists:
            postgres_source.mkdir(parents=True)
            marker.write_text("synthetic configuration\n", encoding="utf-8")

        completed = subprocess.run(
            [str(launcher)],
            env={
                "HOME": str(home),
                "MERLIN_INSTALL_NO_LAUNCH": "1",
                "PATH": "/usr/bin:/bin",
            },
            text=True,
            capture_output=True,
        )

        assert completed.returncode == 0, completed.stderr
        installed = home / ".local" / "share" / "merlin" / release.name
        assert (home / ".local" / "bin" / "merlin").is_symlink()
        if source_exists:
            assert not postgres_source.is_symlink()
            assert marker.read_text(encoding="utf-8") == "synthetic configuration\n"
            assert not (postgres_source / "18.1.0").exists()
        else:
            assert postgres_source.is_symlink()
            assert (
                postgres_source.resolve()
                == (installed / "payload" / "postgresql" / "18.1.0").resolve()
            )
