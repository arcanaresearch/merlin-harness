#!/usr/bin/env python3
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from merlin_packs._bundle_crypto import decrypt_builtin_pack
from merlin_packs.knowledge import PACK_FILES, _load_pack


def _verify_payload(name: str, payload: bytes) -> None:
    plaintext = decrypt_builtin_pack(name, payload)
    _load_pack(plaintext)


def _verify_members(members: dict[str, bytes], *, source: Path) -> None:
    plaintext = sorted(name for name in members if name.endswith(".rpk"))
    if plaintext:
        raise ValueError(f"plaintext knowledge packs found in {source}: {plaintext}")

    encrypted = {
        Path(name).name.removesuffix(".enc"): payload
        for name, payload in members.items()
        if name.endswith(".rpk.enc")
    }
    expected = set(PACK_FILES)
    observed = set(encrypted)
    if observed != expected:
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        raise ValueError(
            f"encrypted knowledge-pack set mismatch in {source}; "
            f"missing={missing}, unexpected={unexpected}"
        )
    for name in PACK_FILES:
        _verify_payload(name, encrypted[name])


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {
            name: archive.read(name)
            for name in archive.namelist()
            if "/knowledge/" in name and name.endswith((".rpk", ".rpk.enc"))
        }
    _verify_members(members, source=path)


def verify_collector(root: Path) -> None:
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith((".rpk", ".rpk.enc"))
    ]
    names = [path.name for path in paths]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate knowledge-pack assets found in {root}")
    _verify_members(
        {str(path.relative_to(root)): path.read_bytes() for path in paths},
        source=root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify encrypted Merlin knowledge packs in release artifacts."
    )
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--collector-root", type=Path)
    args = parser.parse_args()
    if args.wheel is None and args.collector_root is None:
        parser.error("at least one of --wheel or --collector-root is required")
    if args.wheel is not None:
        verify_wheel(args.wheel)
    if args.collector_root is not None:
        verify_collector(args.collector_root)


if __name__ == "__main__":
    main()
