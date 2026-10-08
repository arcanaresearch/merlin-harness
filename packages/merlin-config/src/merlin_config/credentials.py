from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType

from .config import ConfigPaths, _require_directory


class Credentials(Mapping[str, str]):
    """A read-only mapping whose representation never reveals secret values."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] = ()) -> None:
        self._values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Credentials(keys={sorted(self._values)!r})"


class CredentialStore:
    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self._paths = paths or ConfigPaths.default()

    def load(self, environ: Mapping[str, str] | None = None) -> Credentials:
        environ = os.environ if environ is None else environ
        values: dict[str, str] = {}
        path = self._paths.credentials
        if path.exists():
            _require_directory(self._paths.directory)
            _require_mode(path, 0o600)
            decoded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(decoded, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
            ):
                raise ValueError("credentials.json must contain a JSON object of strings")
            values.update(decoded)

        prefix = "MERLIN_CREDENTIAL_"
        values.update(
            (key.removeprefix(prefix).lower(), value)
            for key, value in environ.items()
            if key.startswith(prefix)
        )
        return Credentials(values)

    def save(self, credentials: Mapping[str, str]) -> None:
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in credentials.items()
        ):
            raise TypeError("credential names and values must be strings")

        directory = self._paths.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)

        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials-", dir=directory)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(dict(credentials), temporary, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self._paths.credentials)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)


def _require_mode(path: Path, expected: int) -> None:
    actual = path.stat().st_mode & 0o777
    if actual != expected:
        raise PermissionError(f"{path} must have mode {expected:04o}, not {actual:04o}")
