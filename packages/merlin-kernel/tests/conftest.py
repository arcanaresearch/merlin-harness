import pytest


@pytest.fixture(autouse=True)
def _no_jev_network(monkeypatch):
    """Unit tests never consult real Jev config or transport."""
    monkeypatch.setattr("merlin_kernel.kernel._load_jev_client", lambda: None)
