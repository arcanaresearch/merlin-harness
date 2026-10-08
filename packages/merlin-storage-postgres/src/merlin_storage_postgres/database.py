import atexit
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row


class _SerializedTransaction(AbstractContextManager[Any]):
    def __init__(self, lock: RLock, transaction: AbstractContextManager[Any]) -> None:
        self._lock = lock
        self._transaction = transaction

    def __enter__(self) -> Any:
        self._lock.acquire()
        try:
            return self._transaction.__enter__()
        except BaseException:
            self._lock.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return self._transaction.__exit__(exc_type, exc_value, traceback)
        finally:
            self._lock.release()


class _SerializedConnection:
    """Prevent repositories sharing one psycopg connection from nesting transactions."""

    def __init__(self, connection: Connection[dict[str, Any]]) -> None:
        self._connection = connection
        self._merlin_operation_lock = RLock()

    def transaction(self, *args: Any, **kwargs: Any) -> _SerializedTransaction:
        return _SerializedTransaction(
            self._merlin_operation_lock,
            self._connection.transaction(*args, **kwargs),
        )

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        with self._merlin_operation_lock:
            return self._connection.execute(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._connection.cursor(*args, **kwargs)

    def close(self) -> None:
        with self._merlin_operation_lock:
            self._connection.close()

    def __enter__(self) -> Self:
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        with self._merlin_operation_lock:
            return self._connection.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class EmbeddedPostgres:
    """Small lifecycle adapter around pg0's embedded PostgreSQL server."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._name = f"merlin-{sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:12]}"
        self._server: Any | None = None
        self._uri: str | None = None
        self._owns_server = False

    def start(self) -> str:
        from pg0 import Pg0, Pg0AlreadyRunningError

        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._server = Pg0(name=self._name, data_dir=str(self.data_dir))
        previous_locale = os.environ.get("LC_ALL")
        if sys.platform == "darwin":
            # pg0 delegates cluster initialization to initdb, which rejects the
            # mixed LANG=en_US.UTF-8 / LC_ALL=C environment used by some shells.
            os.environ["LC_ALL"] = "en_US.UTF-8"
        try:
            try:
                self._server.start()
                self._owns_server = True
            except Pg0AlreadyRunningError:
                # The deterministic name and data directory identify the exact
                # Merlin postmaster, but a postmaster this process did not start
                # belongs to whoever started it (the shared `merlin start`
                # instance, a live campaign, a TUI window). Connect, never
                # adopt: cleanup must not stop a server another process owns.
                if not self._server.running:
                    raise
                self._owns_server = False
        except BaseException:
            # A failed start can still leave a pg0 instance registration behind.
            self._discard_registration()
            self._server = None
            raise
        finally:
            if sys.platform == "darwin":
                if previous_locale is None:
                    os.environ.pop("LC_ALL", None)
                else:
                    os.environ["LC_ALL"] = previous_locale
        uri = self._server.uri
        if not uri:
            if self._owns_server:
                self._server.stop()
                self._discard_registration()
            self._server = None
            raise RuntimeError("embedded PostgreSQL is running without a connection URI")
        self._uri = str(uri)
        if self._owns_server:
            _owned_embedded_instances.add(self)
        return self._uri

    def stop(self) -> None:
        if self._server is None:
            return
        uri = self._uri
        owned = self._owns_server
        if owned:
            # If the underlying stop fails, keep the server reference, the pg0
            # registration, and the atexit tracking so a later stop()/atexit can
            # retry instead of abandoning a potentially running server.
            self._server.stop()
            # pg0 keeps a registration under ~/.pg0/instances/<name> that stop()
            # never removes; thousands of stale registrations degrade later
            # startups. Drop only the registration — the data_dir must survive
            # so clusters can restart.
            self._discard_registration()
        self._server = None
        self._uri = None
        self._owns_server = False
        if uri is not None and owned:
            _wait_for_listener_release(uri)

    def _discard_registration(self) -> None:
        _owned_embedded_instances.discard(self)
        registration = Path.home() / ".pg0" / "instances" / self._name
        if (registration / "instance.json").is_file():
            shutil.rmtree(registration, ignore_errors=True)


# Strong references: an instance stays tracked until its stop() succeeds and
# discards it, so a failed stop cannot strand a running server unreachable by
# the atexit backstop. Successfully stopped instances are removed here.
_owned_embedded_instances: set[EmbeddedPostgres] = set()


@atexit.register
def _stop_owned_embedded_instances() -> None:
    """Best-effort cleanup for interpreters exiting without an explicit stop()."""
    for embedded in list(_owned_embedded_instances):
        try:
            embedded.stop()
        except BaseException:
            pass


def _wait_for_listener_release(uri: str, *, timeout: float = 5.0) -> None:
    """Wait until pg0's postmaster has actually released its TCP listener."""
    parsed = urlsplit(uri)
    if parsed.hostname is None or parsed.port is None:
        return
    # pg0 is always local. Use a direct IPv4 socket and a numeric byte address:
    # frozen Python can begin codec teardown before this final resource closes,
    # making the hostname/IDNA path used by create_connection unavailable.
    address = (b"127.0.0.1", parsed.port)
    deadline = time.monotonic() + timeout
    while True:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(0.05)
        try:
            connection.connect(address)
        except OSError:
            return
        finally:
            connection.close()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"embedded PostgreSQL listener did not stop within {timeout:.1f}s")
        time.sleep(0.01)


def _default_data_dir() -> Path:
    preferred = Path.home() / ".merlin" / "postgres"
    legacy = Path.home() / ".redpen-python" / "postgres"
    # Adopt the pre-rename embedded cluster when it holds data and the
    # preferred location was never initialized.
    if not (preferred / "PG_VERSION").exists() and (legacy / "PG_VERSION").exists():
        return legacy
    return preferred


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    url: str | None = None
    data_dir: Path = field(default_factory=_default_data_dir)

    @classmethod
    def from_environment(cls) -> Self:
        return cls(url=os.environ.get("MERLIN_DATABASE_URL"))


class Database:
    """Owns connection creation and the embedded PostgreSQL process."""

    def __init__(
        self,
        config: DatabaseConfig | None = None,
        *,
        embedded_factory: Callable[[Path], EmbeddedPostgres] = EmbeddedPostgres,
    ) -> None:
        self.config = config or DatabaseConfig.from_environment()
        self._embedded_factory = embedded_factory
        self._embedded: EmbeddedPostgres | None = None
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("database has not been started")
        return self._url

    def start(self) -> Self:
        if self._url is not None:
            return self
        if self.config.url:
            self._url = self.config.url
        else:
            self._embedded = self._embedded_factory(self.config.data_dir)
            self._url = self._embedded.start()
        return self

    def connect(self) -> _SerializedConnection:
        connection = psycopg.connect(self.url, row_factory=dict_row)
        connection.autocommit = True
        return _SerializedConnection(connection)

    def stop(self) -> None:
        if self._embedded is not None:
            self._embedded.stop()
            self._embedded = None
        self._url = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
