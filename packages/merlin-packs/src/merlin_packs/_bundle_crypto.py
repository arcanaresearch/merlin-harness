"""Level-1 protection for bundled knowledge assets.

The executable must be able to recover this key, so this is deliberately an
obfuscation boundary rather than protection from a determined local debugger.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"MERLIN-RPK-AESGCM-V1\n"
_KEY_PART_A = bytes.fromhex("08438e91e88653f352a1ff5c8a88d4c408a7edcf126112ea09ce822c619cc335")
_KEY_PART_B = bytes.fromhex("85bc89a3f3f5a034b6fa1ad647a3d61fe4ed594161ae23dbc034d9ecb6533c81")


def decrypt_builtin_pack(name: str, payload: bytes) -> str:
    if not payload.startswith(_MAGIC) or len(payload) <= len(_MAGIC) + 12:
        raise ValueError(f"bundled knowledge pack {name} is not encrypted")
    nonce_start = len(_MAGIC)
    nonce = payload[nonce_start : nonce_start + 12]
    ciphertext = payload[nonce_start + 12 :]
    key = bytes(left ^ right for left, right in zip(_KEY_PART_A, _KEY_PART_B, strict=True))
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, name.encode())
    return plaintext.decode("utf-8")
