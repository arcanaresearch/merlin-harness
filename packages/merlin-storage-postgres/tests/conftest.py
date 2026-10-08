import json
import warnings
from pathlib import Path

import pytest

_PG0_INSTANCES_DIR = Path.home() / ".pg0" / "instances"


@pytest.fixture(autouse=True)
def _force_stop_stray_pg0(tmp_path: Path):
    """Force-stop pg0 postmasters rooted in this test's tmp_path at teardown.

    Restart tests deliberately drop and reattach embedded clusters; when a test
    fails (or a run is killed) mid-test, the postmaster can outlive pytest and
    later runs reattach to the stale server, failing under random ordering.
    Database.stop() only stops servers it owns, so sweep the pg0 registry for
    instances whose data_dir lives under this test's tmp_path and stop them
    unconditionally, whether the test passed or failed.
    """
    yield
    if not _PG0_INSTANCES_DIR.is_dir():
        return
    from pg0 import Pg0

    for entry in _PG0_INSTANCES_DIR.iterdir():
        try:
            info = json.loads((entry / "instance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data_dir = str(info.get("data_dir", ""))
        if not data_dir:
            continue
        try:
            if not Path(data_dir).resolve().is_relative_to(tmp_path.resolve()):
                continue
        except OSError:
            continue
        try:
            server = Pg0(name=entry.name, data_dir=data_dir)
            if server.running:
                server.stop()
        except Exception:  # noqa: BLE001 - teardown must never fail the test
            pass


def _merlin_pg0_registrations() -> set[str]:
    if not _PG0_INSTANCES_DIR.is_dir():
        return set()
    return {
        entry.name for entry in _PG0_INSTANCES_DIR.iterdir() if entry.name.startswith("merlin-")
    }


@pytest.fixture(autouse=True, scope="session")
def _pg0_registration_leak_guard():
    """Warn about pg0 instance registrations leaked by this test session."""
    before = _merlin_pg0_registrations()
    yield
    leaked = sorted(_merlin_pg0_registrations() - before)
    if leaked:
        warnings.warn(
            f"pg0 instance registrations leaked by this session: {leaked}",
            stacklevel=1,
        )
