from __future__ import annotations

import httpx
import pytest
from merlin_tui.provider_catalog import (
    PROVIDER_ROUTES,
    ModelCandidate,
    ModelDiscovery,
    discover_model_catalog,
    discover_routing_models,
)


def test_live_catalog_projects_current_model_capabilities(monkeypatch) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        json={
            "data": [
                {
                    "id": "vendor/current-model",
                    "context_length": 262_144,
                    "architecture": {"input_modalities": ["text", "image"]},
                }
            ]
        },
    )
    monkeypatch.setattr("merlin_tui.provider_catalog.httpx.get", lambda *args, **kwargs: response)

    discovered = discover_model_catalog(PROVIDER_ROUTES[5], "opaque-secret")

    assert discovered.error is None
    assert discovered.models[0].id == "vendor/current-model"
    assert discovered.models[0].vision is True
    assert discovered.models[0].context_window_tokens == 262_144


def test_live_catalog_reports_authentication_failure_without_exposing_secret(monkeypatch) -> None:
    request = httpx.Request("GET", "https://api.example.test/v1/models")
    response = httpx.Response(401, request=request)

    def reject(*args, **kwargs):
        raise httpx.HTTPStatusError("opaque-secret", request=request, response=response)

    monkeypatch.setattr("merlin_tui.provider_catalog.httpx.get", reject)
    discovered = discover_model_catalog(
        PROVIDER_ROUTES[-1], "opaque-secret", base_url="https://api.example.test/v1"
    )

    assert discovered.models == ()
    assert (
        discovered.error == "Model discovery was rejected; verify the API key and provider account."
    )
    assert "opaque-secret" not in discovered.error


@pytest.mark.asyncio
async def test_routing_catalogs_keep_models_separated_by_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "merlin_tui.provider_catalog.discover_model_catalog",
        lambda *args, **kwargs: ModelDiscovery((ModelCandidate("worker-current"),)),
    )

    async def codex_models():
        return ModelDiscovery((ModelCandidate("gpt-current"),))

    monkeypatch.setattr("merlin_tui.provider_catalog.discover_codex_models", codex_models)

    discovered = await discover_routing_models(
        {"base_url": "https://api.example.test/v1"}, "opaque-secret"
    )

    assert discovered["openai_compatible"].models[0].id == "worker-current"
    assert discovered["codex_app_server"].models[0].id == "gpt-current"
    assert discovered["claude_cli"].models == ()
