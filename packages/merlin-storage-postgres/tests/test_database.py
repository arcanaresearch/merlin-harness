import socket
from pathlib import Path

import pytest
from merlin_storage_postgres import Database, DatabaseConfig
from merlin_storage_postgres.database import (
    EmbeddedPostgres,
    _owned_embedded_instances,
    _wait_for_listener_release,
)


class FakeEmbedded:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.stopped = False

    def start(self) -> str:
        return "postgresql://embedded/postgres"

    def stop(self) -> None:
        self.stopped = True


def test_external_url_does_not_start_embedded() -> None:
    def unexpected(_: Path) -> FakeEmbedded:
        raise AssertionError("embedded PostgreSQL should not start")

    database = Database(
        DatabaseConfig(url="postgresql://external/merlin"), embedded_factory=unexpected
    )
    with database:
        assert database.url == "postgresql://external/merlin"


def test_config_reads_external_url(monkeypatch) -> None:
    monkeypatch.setenv("MERLIN_DATABASE_URL", "postgresql://configured/merlin")
    assert DatabaseConfig.from_environment().url == "postgresql://configured/merlin"


def test_embedded_lifecycle() -> None:
    instance = FakeEmbedded(Path("unused"))
    database = Database(
        DatabaseConfig(data_dir=Path("cluster")), embedded_factory=lambda _: instance
    )
    database.start()
    assert database.url == "postgresql://embedded/postgres"
    database.stop()
    assert instance.stopped


def test_embedded_stop_waits_for_listener_release(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    class Server:
        uri = "postgresql://postgres:postgres@127.0.0.1:5433/postgres"

        def start(self) -> None:
            return None

        def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr("pg0.Pg0", lambda **_: Server())
    monkeypatch.setattr(
        "merlin_storage_postgres.database._wait_for_listener_release",
        lambda uri: calls.append(uri),
    )
    embedded = EmbeddedPostgres(tmp_path / "postgres")
    uri = embedded.start()
    embedded.stop()

    assert calls == ["stop", uri]


def test_listener_release_uses_numeric_loopback_for_frozen_shutdown(monkeypatch) -> None:
    events: list[object] = []

    class ReleasedSocket:
        def __init__(self, family: int, kind: int) -> None:
            events.append((family, kind))

        def settimeout(self, timeout: float) -> None:
            events.append(timeout)

        def connect(self, address: tuple[bytes, int]) -> None:
            events.append(address)
            raise ConnectionRefusedError

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr("socket.socket", ReleasedSocket)
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: pytest.fail("hostname lookup path must not be used"),
    )

    _wait_for_listener_release("postgresql://postgres@localhost:5433/postgres")

    assert events == [
        (socket.AF_INET, socket.SOCK_STREAM),
        0.05,
        (b"127.0.0.1", 5433),
        "closed",
    ]


def test_embedded_reattaches_to_running_pg0_without_adopting_it(
    monkeypatch, tmp_path: Path
) -> None:
    """A postmaster this process did not start (the shared instance, a live
    campaign's postgres, another TUI window) is connected to but never stopped
    or deregistered by this process's cleanup."""
    from pg0 import Pg0AlreadyRunningError

    calls: list[str] = []

    class Server:
        running = True
        uri = "postgresql://postgres:postgres@127.0.0.1:5433/postgres"

        def start(self) -> None:
            raise Pg0AlreadyRunningError("already running")

        def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr("pg0.Pg0", lambda **_: Server())
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "merlin_storage_postgres.database._wait_for_listener_release", lambda _uri: None
    )
    embedded = EmbeddedPostgres(tmp_path / "postgres")

    assert embedded.start() == Server.uri
    assert embedded not in _owned_embedded_instances
    embedded.stop()

    assert calls == []


def test_embedded_started_by_us_is_stopped_on_cleanup(monkeypatch, tmp_path: Path) -> None:
    """The owning process keeps full lifecycle: start tracks ownership, stop
    stops the server and reaps its pg0 registration."""
    calls: list[str] = []

    class Server:
        running = True
        uri = "postgresql://postgres:postgres@127.0.0.1:5433/postgres"

        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr("pg0.Pg0", lambda **_: Server())
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "merlin_storage_postgres.database._wait_for_listener_release", lambda _uri: None
    )
    embedded = EmbeddedPostgres(tmp_path / "postgres")
    embedded.start()
    registration = tmp_path / ".pg0" / "instances" / embedded._name
    registration.mkdir(parents=True)
    (registration / "instance.json").write_text("{}")

    assert embedded in _owned_embedded_instances
    embedded.stop()

    assert calls == ["start", "stop"]
    assert not registration.exists()
    assert embedded not in _owned_embedded_instances


class _FakeServer:
    uri = "postgresql://postgres:postgres@127.0.0.1:5433/postgres"

    def __init__(self, failures: int = 0) -> None:
        self._failures = failures

    def start(self) -> None:
        return None

    def stop(self) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("stop failed")


def _prepare_owned_embedded(monkeypatch, tmp_path: Path, server: _FakeServer) -> EmbeddedPostgres:
    monkeypatch.setattr("pg0.Pg0", lambda **_: server)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "merlin_storage_postgres.database._wait_for_listener_release", lambda uri: None
    )
    embedded = EmbeddedPostgres(tmp_path / "postgres")
    embedded.start()
    registration = tmp_path / ".pg0" / "instances" / embedded._name
    registration.mkdir(parents=True)
    (registration / "instance.json").write_text("{}")
    return embedded


def test_failed_stop_retains_registration_and_tracking(monkeypatch, tmp_path: Path) -> None:
    embedded = _prepare_owned_embedded(monkeypatch, tmp_path, _FakeServer(failures=1))
    registration = tmp_path / ".pg0" / "instances" / embedded._name

    with pytest.raises(RuntimeError, match="stop failed"):
        embedded.stop()

    assert (registration / "instance.json").is_file()
    assert embedded._server is not None
    assert embedded in _owned_embedded_instances

    embedded.stop()

    assert not registration.exists()
    assert embedded._server is None
    assert embedded not in _owned_embedded_instances


def test_successful_stop_removes_registration_and_untracks(monkeypatch, tmp_path: Path) -> None:
    embedded = _prepare_owned_embedded(monkeypatch, tmp_path, _FakeServer())
    registration = tmp_path / ".pg0" / "instances" / embedded._name

    embedded.stop()

    assert not registration.exists()
    assert embedded._server is None
    assert embedded not in _owned_embedded_instances
