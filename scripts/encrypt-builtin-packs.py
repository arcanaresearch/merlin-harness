#!/usr/bin/env python3
"""Regenerate encrypted bundled RPK assets after an intentional pack update."""

from __future__ import annotations

import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = ROOT / "packages/merlin-packs/src/merlin_packs/knowledge"
MAGIC = b"MERLIN-RPK-AESGCM-V1\n"
KEY = bytes.fromhex("8dff07321b73f3c7e45be58acd2b02dbec4ab48e73cf3131c9fa5bc0d7cfffb4")


def main() -> None:
    for source in sorted(PACK_ROOT.glob("*.rpk")):
        nonce = secrets.token_bytes(12)
        encrypted = (
            MAGIC + nonce + AESGCM(KEY).encrypt(nonce, source.read_bytes(), source.name.encode())
        )
        destination = source.with_suffix(source.suffix + ".enc")
        destination.write_bytes(encrypted)
        print(f"encrypted {source.name} -> {destination.name}")


if __name__ == "__main__":
    main()
