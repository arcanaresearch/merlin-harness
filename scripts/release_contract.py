#!/usr/bin/env python3
"""Stamp and verify the identity shared by every standalone release surface."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
from pathlib import Path

VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)?$")


def stamp(manifest: Path, *, version: str, name: str, platform: str, channel: str) -> None:
    _validate(version, name, platform, channel)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["release"] = {
        "archive": f"{name}.tar.gz",
        "channel": channel,
        "name": name,
        "platform": platform,
        "version": version,
    }
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify(
    manifest: Path,
    *,
    version: str,
    name: str,
    platform: str,
    channel: str,
    binary: Path | None = None,
    archive: Path | None = None,
) -> None:
    _validate(version, name, platform, channel)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    expected = {
        "archive": f"{name}.tar.gz",
        "channel": channel,
        "name": name,
        "platform": platform,
        "version": version,
    }
    if document.get("release") != expected:
        raise SystemExit(f"release manifest mismatch: expected {expected!r}")
    if manifest.parent.name != name:
        raise SystemExit(f"release folder {manifest.parent.name!r} does not match {name!r}")
    channel_path = manifest.parent / "release-channel"
    if channel_path.read_text(encoding="utf-8").strip() != channel:
        raise SystemExit("release-channel does not match manifest")
    if binary is not None:
        observed = subprocess.run(
            [str(binary), "--version"], text=True, capture_output=True, check=True
        ).stdout.strip()
        if observed != f"merlin {version}":
            raise SystemExit(f"binary version mismatch: {observed!r}")
    if archive is not None:
        if archive.name != expected["archive"]:
            raise SystemExit("archive filename does not match manifest")
        with tarfile.open(archive, "r:gz") as bundle:
            names = bundle.getnames()
        if not names or any(item != name and not item.startswith(f"{name}/") for item in names):
            raise SystemExit("archive contains paths outside its versioned release directory")


def _validate(version: str, name: str, platform: str, channel: str) -> None:
    if not VERSION.fullmatch(version):
        raise SystemExit(f"invalid release version: {version!r}")
    if name != f"merlin-{version}-{platform}":
        raise SystemExit("release name must be derived from version and platform")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", channel):
        raise SystemExit(f"invalid release channel: {channel!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stamp", "verify"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    values = {
        "version": args.version,
        "name": args.name,
        "platform": args.platform,
        "channel": args.channel,
    }
    if args.action == "stamp":
        stamp(args.manifest, **values)
    else:
        verify(args.manifest, binary=args.binary, archive=args.archive, **values)


if __name__ == "__main__":
    main()
