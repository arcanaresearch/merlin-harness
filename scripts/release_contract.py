#!/usr/bin/env python3
"""Stamp and verify the identity shared by every standalone release surface."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import subprocess
import tarfile
from pathlib import Path

VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)?$")


def _contained_archive_path(value: str, release_name: str) -> str:
    if posixpath.isabs(value):
        raise SystemExit(f"release archive contains absolute path: {value}")
    normalized = posixpath.normpath(value)
    if (
        normalized in {".", ".."}
        or posixpath.isabs(normalized)
        or (normalized != release_name and not normalized.startswith(f"{release_name}/"))
    ):
        raise SystemExit(f"release archive contains path outside bundle: {value}")
    return normalized


def _archive_member_path(value: str, release_name: str) -> str:
    if any(part in {".", ".."} for part in value.split("/")):
        raise SystemExit(f"release archive contains unsafe path: {value}")
    return _contained_archive_path(value, release_name)


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
    release_root = manifest.parent
    for path in release_root.rglob("*"):
        if not path.is_symlink():
            continue
        link_target = os.readlink(path)
        if posixpath.isabs(link_target):
            raise SystemExit(f"release bundle contains absolute symlink: {path}")
        relative_path = path.relative_to(release_root).as_posix()
        lexical_target = posixpath.normpath(
            posixpath.join(posixpath.dirname(relative_path), link_target)
        )
        if lexical_target == ".." or lexical_target.startswith("../"):
            raise SystemExit(f"release bundle symlink escapes bundle: {path}")
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
            members = bundle.getmembers()
        if not members:
            raise SystemExit("release archive is empty")
        normalized_names = {
            id(member): _archive_member_path(member.name, name) for member in members
        }
        for member in members:
            if not (member.issym() or member.islnk()):
                continue
            if member.linkname.startswith("/"):
                raise SystemExit(f"release archive contains absolute link: {member.name}")
            base = posixpath.dirname(normalized_names[id(member)]) if member.issym() else ""
            try:
                _contained_archive_path(posixpath.join(base, member.linkname), name)
            except SystemExit as error:
                raise SystemExit(f"release archive link escapes bundle: {member.name}") from error


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
