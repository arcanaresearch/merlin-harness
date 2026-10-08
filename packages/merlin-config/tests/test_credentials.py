import json
from pathlib import Path

import pytest
from merlin_config import ConfigPaths, CredentialStore


def test_save_uses_owner_only_permissions_and_loads_values(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    store = CredentialStore(paths)

    store.save({"openai_api_key": "secret"})

    assert paths.directory.stat().st_mode & 0o777 == 0o700
    assert paths.credentials.stat().st_mode & 0o777 == 0o600
    assert store.load({})["openai_api_key"] == "secret"


def test_save_atomically_replaces_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = ConfigPaths(tmp_path)
    store = CredentialStore(paths)
    store.save({"token": "old"})
    replacements: list[tuple[str, Path]] = []

    original_replace = __import__("os").replace

    def recording_replace(source: str, destination: Path) -> None:
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr("merlin_config.credentials.os.replace", recording_replace)
    store.save({"token": "new"})

    assert replacements and replacements[0][1] == paths.credentials
    assert json.loads(paths.credentials.read_text()) == {"token": "new"}


def test_environment_credentials_override_file_and_repr_is_redacted(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    store = CredentialStore(paths)
    store.save({"api_key": "file-secret"})

    credentials = store.load({"MERLIN_CREDENTIAL_API_KEY": "environment-secret"})

    assert credentials["api_key"] == "environment-secret"
    assert "environment-secret" not in repr(credentials)
    assert "file-secret" not in repr(credentials)


def test_rejects_credentials_with_insecure_permissions(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.credentials.write_text("{}", encoding="utf-8")
    paths.credentials.chmod(0o644)

    with pytest.raises(PermissionError):
        CredentialStore(paths).load({})


def test_rejects_insecure_credentials_directory(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    CredentialStore(paths).save({})
    paths.directory.chmod(0o755)

    with pytest.raises(PermissionError):
        CredentialStore(paths).load({})
