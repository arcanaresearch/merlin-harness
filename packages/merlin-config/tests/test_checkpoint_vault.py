import json
from pathlib import Path

import pytest
from merlin_config import ConfigPaths, CredentialCheckpointVault


def test_checkpoint_vault_keeps_secret_behind_opaque_reference(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    vault = CredentialCheckpointVault(paths)

    entry = vault.store_checkpoint("target-secret-token", campaign_id="campaign/1", gate_id="login")

    assert entry.opaque_reference() == {
        "kind": "opaque_reference",
        "namespace": "state",
        "reference_id": entry.reference_id,
    }
    assert entry.keyed_digest()["algorithm"] == "hmac-sha256"
    assert vault.resolve_checkpoint(entry.reference_id) == "target-secret-token"
    principal = vault.digest_identity({"account": "alice"}, purpose="target-principal")
    assert principal["kind"] == "keyed_digest"
    assert principal == vault.digest_identity({"account": "alice"}, purpose="target-principal")
    assert principal != vault.digest_identity({"account": "alice"}, purpose="physical-state")
    assert paths.credentials.stat().st_mode & 0o777 == 0o600
    assert "target-secret-token" not in repr(entry)


def test_checkpoint_vault_is_idempotent_and_rejects_binding_overwrite(tmp_path: Path) -> None:
    vault = CredentialCheckpointVault(ConfigPaths(tmp_path))

    first = vault.store_checkpoint("secret-a", campaign_id="campaign/1", gate_id="login")
    replay = vault.store_checkpoint("secret-a", campaign_id="campaign/1", gate_id="login")

    assert replay == first
    with pytest.raises(ValueError, match="another secret"):
        vault.store_checkpoint("secret-b", campaign_id="campaign/1", gate_id="login")


def test_checkpoint_vault_detects_tampering_and_fences_delete(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    vault = CredentialCheckpointVault(paths)
    entry = vault.store_checkpoint("secret-a", campaign_id="campaign/1", gate_id="login")

    with pytest.raises(ValueError, match="does not match"):
        vault.delete_checkpoint(entry.reference_id, expected_digest="0" * 64)

    document = json.loads(paths.credentials.read_text())
    name = next(key for key in document if key.startswith("merlin_checkpoint_vault_entry_"))
    payload = json.loads(document[name])
    payload["secret"] = "tampered"
    document[name] = json.dumps(payload)
    paths.credentials.write_text(json.dumps(document), encoding="utf-8")
    paths.credentials.chmod(0o600)

    with pytest.raises(ValueError, match="integrity"):
        vault.resolve_checkpoint(entry.reference_id)
