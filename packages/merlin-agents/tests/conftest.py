"""Shared fixtures for merlin-agents tests."""

from __future__ import annotations

import pytest
from merlin_agents.campaign_service import CampaignService
from merlin_agents.durable_service import DurableAgentService


@pytest.fixture(autouse=True)
def _no_ambient_jev_client(monkeypatch: pytest.MonkeyPatch):
    """Keep tests hermetic: ambient Jev config must never fire in tests.

    The lazily constructed escalation-gate client reads operator config; a
    machine with a live Jev token would otherwise let advisory gates make real
    network decisions inside unrelated tests.  Tests opt in by attaching a
    client explicitly (``_jev_client`` + ``_jev_client_checked``) or by
    monkeypatching the method on the instance.
    """

    def hermetic(self):  # noqa: ANN001, ANN202
        if self._jev_client_checked:
            return self._jev_client
        return None

    monkeypatch.setattr(CampaignService, "_jev_escalation_client", hermetic)


@pytest.fixture(autouse=True)
def _no_ambient_jev_shadow_client(monkeypatch: pytest.MonkeyPatch):
    """Same hermetic guarantee for the novelty-shadow client in discovery."""
    monkeypatch.setattr("merlin_agents.discovery._shadow_client_checked", True)
    monkeypatch.setattr("merlin_agents.discovery._shadow_client", None)


@pytest.fixture(autouse=True)
def _no_ambient_jev_severity_client(monkeypatch: pytest.MonkeyPatch):
    """Same hermetic guarantee for the finding-severity client in durable intake."""

    def hermetic(self):  # noqa: ANN001, ANN202
        if self._jev_client_checked:
            return self._jev_client
        return None

    monkeypatch.setattr(DurableAgentService, "_jev_severity_client", hermetic)
