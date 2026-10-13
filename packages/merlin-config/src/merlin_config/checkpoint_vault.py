"""Owner-only storage for target checkpoint secrets."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .config import ConfigPaths, _require_directory
from .credentials import CredentialStore

_KEY_NAME = "merlin_checkpoint_vault_hmac_v1"
_ENTRY_PREFIX = "merlin_checkpoint_vault_entry_"
_KEY_ID = "local-credentials-v1"


@dataclass(frozen=True, slots=True)
class CheckpointVaultEntry:
    """Secret-safe identifiers returned after storing a checkpoint token."""

    reference_id: str
    key_id: str
    digest: str

    def opaque_reference(self) -> dict[str, str]:
        return {
            "kind": "opaque_reference",
            "namespace": "state",
            "reference_id": self.reference_id,
        }

    def keyed_digest(self) -> dict[str, str]:
        return {
            "kind": "keyed_digest",
            "algorithm": "hmac-sha256",
            "key_id": self.key_id,
            "digest": self.digest,
        }


class CredentialCheckpointVault:
    """Checkpoint vault backed by Merlin's owner-only credential store.

    PostgreSQL receives only ``opaque_reference()`` and ``keyed_digest()``.
    The target-issued checkpoint token remains in ``credentials.json``.
    """

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self._paths = paths or ConfigPaths.default()
        self._credentials = CredentialStore(self._paths)

    def store_checkpoint(
        self,
        checkpoint_secret: str,
        *,
        campaign_id: str,
        gate_id: str,
    ) -> CheckpointVaultEntry:
        """Store a secret idempotently under its reviewed campaign/gate binding."""

        secret = _nonempty(checkpoint_secret, "checkpoint secret")
        reference_id = _reference_id(campaign_id, gate_id)
        with self._locked_credentials() as values:
            key = _load_or_create_hmac_key(values)
            digest = hmac.new(key, secret.encode(), hashlib.sha256).hexdigest()
            entry_name = _ENTRY_PREFIX + reference_id
            existing = values.get(entry_name)
            if existing is not None:
                document = _decode_entry(existing)
                if not hmac.compare_digest(str(document["digest"]), digest):
                    raise ValueError("checkpoint vault binding already contains another secret")
                if not hmac.compare_digest(str(document["secret"]), secret):
                    raise ValueError("checkpoint vault entry failed integrity validation")
            else:
                values[entry_name] = json.dumps(
                    {"digest": digest, "key_id": _KEY_ID, "secret": secret},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                self._credentials.save(values)
        return CheckpointVaultEntry(reference_id, _KEY_ID, digest)

    def resolve_checkpoint(self, reference_id: str) -> str:
        """Resolve an internal opaque reference without exposing it in durable telemetry."""

        reference = _valid_reference(reference_id)
        with self._locked_credentials() as values:
            encoded = values.get(_ENTRY_PREFIX + reference)
            if encoded is None:
                raise KeyError(reference)
            document = _decode_entry(encoded)
            key = _load_hmac_key(values)
            secret = str(document["secret"])
            expected = hmac.new(key, secret.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(document["digest"]), expected):
                raise ValueError("checkpoint vault entry failed integrity validation")
            return secret

    def digest_identity(self, value: object, *, purpose: str) -> dict[str, str]:
        """Return a stable keyed digest without retaining the selected raw value."""

        context = _nonempty(purpose, "identity digest purpose")
        encoded = json.dumps(
            {"purpose": context, "value": value},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        with self._locked_credentials() as values:
            created = _KEY_NAME not in values
            key = _load_or_create_hmac_key(values)
            if created:
                self._credentials.save(values)
        return {
            "kind": "keyed_digest",
            "algorithm": "hmac-sha256",
            "key_id": _KEY_ID,
            "digest": hmac.new(key, encoded, hashlib.sha256).hexdigest(),
        }

    def delete_checkpoint(self, reference_id: str, *, expected_digest: str) -> bool:
        """Delete an entry only when the caller presents its keyed digest."""

        reference = _valid_reference(reference_id)
        digest = _valid_digest(expected_digest)
        with self._locked_credentials() as values:
            entry_name = _ENTRY_PREFIX + reference
            encoded = values.get(entry_name)
            if encoded is None:
                return False
            document = _decode_entry(encoded)
            if not hmac.compare_digest(str(document["digest"]), digest):
                raise ValueError("checkpoint vault digest does not match")
            del values[entry_name]
            self._credentials.save(values)
            return True

    @contextmanager
    def _locked_credentials(self) -> Iterator[dict[str, str]]:
        directory = self._paths.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        _require_directory(directory)
        lock_path = directory / ".checkpoint-vault.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield dict(self._credentials.load({}))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _reference_id(campaign_id: str, gate_id: str) -> str:
    binding = json.dumps(
        [_nonempty(campaign_id, "campaign id"), _nonempty(gate_id, "gate id")],
        separators=(",", ":"),
    ).encode()
    return "checkpoint_" + hashlib.sha256(binding).hexdigest()[:48]


def _valid_reference(value: str) -> str:
    reference = _nonempty(value, "checkpoint reference")
    if not reference.startswith("checkpoint_") or len(reference) != 59:
        raise ValueError("checkpoint reference is invalid")
    if any(character not in "0123456789abcdef" for character in reference[11:]):
        raise ValueError("checkpoint reference is invalid")
    return reference


def _valid_digest(value: str) -> str:
    digest = _nonempty(value, "checkpoint digest")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("checkpoint digest must be lowercase HMAC-SHA256")
    return digest


def _load_or_create_hmac_key(values: dict[str, str]) -> bytes:
    encoded = values.get(_KEY_NAME)
    if encoded is None:
        key = secrets.token_bytes(32)
        values[_KEY_NAME] = key.hex()
        return key
    return _decode_hmac_key(encoded)


def _load_hmac_key(values: dict[str, str]) -> bytes:
    encoded = values.get(_KEY_NAME)
    if encoded is None:
        raise ValueError("checkpoint vault HMAC key is missing")
    return _decode_hmac_key(encoded)


def _decode_hmac_key(value: str) -> bytes:
    try:
        key = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("checkpoint vault HMAC key is malformed") from error
    if len(key) != 32:
        raise ValueError("checkpoint vault HMAC key is malformed")
    return key


def _decode_entry(value: str) -> dict[str, str]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("checkpoint vault entry is malformed") from error
    if not isinstance(document, dict) or set(document) != {"digest", "key_id", "secret"}:
        raise ValueError("checkpoint vault entry is malformed")
    if document.get("key_id") != _KEY_ID or not all(
        isinstance(item, str) for item in document.values()
    ):
        raise ValueError("checkpoint vault entry is malformed")
    _valid_digest(str(document["digest"]))
    return document


def _nonempty(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized
