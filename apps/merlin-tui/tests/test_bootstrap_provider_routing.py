from pathlib import Path

import pytest
from merlin_tui.bootstrap import _capability_values_for_route, _route_vision_enabled


def test_route_vision_is_exact_per_model_and_transport() -> None:
    provider = {
        "model_capabilities": {
            "text-default": ["tools"],
            "vision-recon": ["vision", "tools"],
        },
    }

    assert not _route_vision_enabled(
        provider,
        "text-default",
        "openai_compatible",
        default_model="text-default",
    )
    assert _route_vision_enabled(
        provider,
        "vision-recon",
        "openai_responses",
        default_model="text-default",
    )
    assert not _route_vision_enabled(
        provider,
        "unknown-worker",
        "openai_compatible",
        default_model="text-default",
    )


def test_text_default_and_vision_recon_construct_exact_provider_capabilities() -> None:
    from merlin_providers import (
        OpenAICompatibleProvider,
        OpenAIResponsesConfig,
        OpenAIResponsesProvider,
        ProviderConfig,
    )

    provider = {"model_capabilities": {"vision-recon": ["vision"]}}
    text_default = OpenAICompatibleProvider(
        ProviderConfig(
            "https://api.example/v1",
            "key",
            "text-default",
            vision=_route_vision_enabled(
                provider,
                "text-default",
                "openai_compatible",
                default_model="text-default",
            ),
        )
    )
    vision_recon = OpenAIResponsesProvider(
        OpenAIResponsesConfig(
            "https://api.example/v1",
            "key",
            "vision-recon",
            vision=_route_vision_enabled(
                provider,
                "vision-recon",
                "openai_responses",
                default_model="text-default",
            ),
        )
    )

    assert text_default.capabilities.vision is False
    assert vision_recon.capabilities.vision is True


def test_route_vision_rejects_image_unsupported_transports() -> None:
    provider = {"model_capabilities": {"vision-model": ["vision", "tools"]}}

    for transport in ("gemini", "codex_app_server", "claude_cli", "kimi_acp"):
        assert not _route_vision_enabled(
            provider,
            "vision-model",
            transport,
            default_model="vision-model",
        )
        route_values = _capability_values_for_route(
            provider,
            "vision-model",
            transport,
            default_model="vision-model",
        )
        assert route_values["vision"] is False
        assert route_values["model_capabilities"] == {"vision-model": ["tools"]}


def test_route_vision_accepts_exact_anthropic_messages_model() -> None:
    provider = {
        "model_capabilities": {
            "claude-haiku-4-5": ["vision", "tools"],
            "claude-text": ["tools"],
        }
    }

    assert _route_vision_enabled(
        provider,
        "claude-haiku-4-5",
        "anthropic_messages",
        default_model="claude-text",
    )
    assert not _route_vision_enabled(
        provider,
        "claude-text",
        "anthropic_messages",
        default_model="claude-text",
    )


def test_legacy_vision_applies_only_to_its_exact_default_model() -> None:
    provider = {"vision": True}

    assert _route_vision_enabled(
        provider,
        "legacy-default",
        "openai_compatible",
        default_model="legacy-default",
    )
    assert not _route_vision_enabled(
        provider,
        "new-route",
        "openai_compatible",
        default_model="legacy-default",
    )


def test_route_vision_uses_exact_non_assumed_catalog_modalities() -> None:
    from merlin_providers import ModelCapabilityRegistry

    class UnavailableRegistry:
        def resolve_model(self, *_args, **_kwargs):
            raise OSError("catalog unavailable")

    registry = ModelCapabilityRegistry(
        builtin_catalog={
            "catalog-vision": {"modalities": ("text", "image")},
            "catalog-text": {"modalities": ("text",)},
        }
    )

    assert _route_vision_enabled(
        {},
        "catalog-vision",
        "openai_compatible",
        default_model="catalog-text",
        capability_registry=registry,
        endpoint="https://api.deepseek.com/v1",
    )
    assert not _route_vision_enabled(
        {},
        "catalog-text",
        "openai_compatible",
        default_model="catalog-text",
        capability_registry=registry,
        endpoint="https://api.deepseek.com/v1",
    )
    assert not _route_vision_enabled(
        {},
        "catalog-unknown",
        "openai_compatible",
        default_model="catalog-text",
        capability_registry=registry,
        endpoint="https://api.deepseek.com/v1",
    )
    assert not _route_vision_enabled(
        {},
        "catalog-unavailable",
        "openai_compatible",
        default_model="catalog-text",
        capability_registry=UnavailableRegistry(),
        endpoint="https://api.deepseek.com/v1",
    )


def test_route_vision_catalogue_never_overrides_transport_or_operator_authority() -> None:
    from merlin_providers import ModelCapabilityRegistry

    vision_registry = ModelCapabilityRegistry(
        builtin_catalog={"catalog-vision": {"modalities": ("text", "image")}}
    )
    text_registry = ModelCapabilityRegistry(
        builtin_catalog={"catalog-text": {"modalities": ("text",)}}
    )

    assert not _route_vision_enabled(
        {},
        "catalog-vision",
        "codex_app_server",
        default_model="catalog-vision",
        capability_registry=vision_registry,
        endpoint="",
    )
    assert _route_vision_enabled(
        {"model_capabilities": {"catalog-text": ["vision"]}},
        "catalog-text",
        "openai_compatible",
        default_model="catalog-text",
        capability_registry=text_registry,
        endpoint="https://api.deepseek.com/v1",
    )


def test_route_vision_accepts_last_good_catalogue_evidence() -> None:
    from merlin_providers import (
        CapabilityClaim,
        ModelCapabilityRegistry,
        RouteIdentity,
        SourceTier,
    )

    route = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.deepseek.com/v1",
        model="cached-vision",
    )
    registry = ModelCapabilityRegistry(
        builtin_catalog={},
        snapshot_claims={
            route: [
                CapabilityClaim(
                    tier=SourceTier.SECONDARY_CATALOGUE,
                    source="snapshot",
                    values={"modalities": ("text", "image")},
                    retrieved_at="2026-10-28T00:00:00+00:00",
                    raw_model_id=route.model,
                )
            ]
        },
    )

    assert _route_vision_enabled(
        {},
        route.model,
        route.transport,
        default_model="other",
        capability_registry=registry,
        endpoint=route.endpoint,
    )


@pytest.mark.asyncio
async def test_build_backend_constructs_text_default_and_vision_recon_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from merlin_config import Config, CredentialStore
    from merlin_storage_postgres import Database, DatabaseConfig
    from merlin_tui.bootstrap import build_backend

    class CapturingOpenAIProvider:
        configs = []

        def __init__(self, config) -> None:
            self.config = config
            type(self).configs.append(config)

        async def close(self) -> None:
            return None

    class CapturingResponsesProvider(CapturingOpenAIProvider):
        configs = []

    class CapturingAnthropicProvider(CapturingOpenAIProvider):
        configs = []

    CapturingOpenAIProvider.configs = []
    CapturingResponsesProvider.configs = []
    CapturingAnthropicProvider.configs = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "https://api.example/v1",
                        "model": "text-default",
                        "model_transports": {
                            "text-default": "openai_compatible",
                            "vision-recon": "openai_responses",
                            "claude-vision": "anthropic_messages",
                        },
                        "role_models": {"recon": "vision-recon"},
                        "model_capabilities": {
                            "vision-recon": ["vision"],
                            "claude-vision": ["vision"],
                        },
                    }
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda _self: {"api_key": "secret"})
        monkeypatch.setattr(
            "merlin_providers.OpenAICompatibleProvider", CapturingOpenAIProvider
        )
        monkeypatch.setattr(
            "merlin_providers.OpenAIResponsesProvider", CapturingResponsesProvider
        )
        monkeypatch.setattr(
            "merlin_providers.AnthropicMessagesProvider", CapturingAnthropicProvider
        )

        backend = build_backend(tmp_path / "workspace")
        try:
            assert [config.vision for config in CapturingOpenAIProvider.configs] == [False, False]
            assert [config.model for config in CapturingOpenAIProvider.configs] == [
                "text-default",
                "text-default",
            ]
            assert [config.vision for config in CapturingResponsesProvider.configs] == [True]
            assert [config.model for config in CapturingResponsesProvider.configs] == [
                "vision-recon"
            ]
            assert [config.vision for config in CapturingAnthropicProvider.configs] == [True]
            assert [config.model for config in CapturingAnthropicProvider.configs] == [
                "claude-vision"
            ]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_build_backend_applies_catalog_modalities_to_concrete_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from merlin_config import Config, CredentialStore
    from merlin_providers import CapabilityClaim, SourceTier
    from merlin_storage_postgres import Database, DatabaseConfig
    from merlin_tui.bootstrap import build_backend

    class Catalog:
        def lookup(self, route):
            return CapabilityClaim(
                tier=SourceTier.SECONDARY_CATALOGUE,
                source="models.dev",
                values={
                    "context_window_tokens": 131_072,
                    "modalities": (
                        ("text", "image") if route.model == "catalog-vision" else ("text",)
                    ),
                },
                retrieved_at="2026-10-28T00:00:00+00:00",
                raw_model_id=route.model,
            )

    class CapturingOpenAIProvider:
        configs = []

        def __init__(self, config) -> None:
            self.config = config
            type(self).configs.append(config)

        async def close(self) -> None:
            return None

    CapturingOpenAIProvider.configs = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "catalog-text",
                        "role_models": {"recon": "catalog-vision"},
                    }
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda _self: {"api_key": "secret"})
        monkeypatch.setattr(
            "merlin_providers.models_dev_catalogue",
            lambda **_kwargs: Catalog(),
        )
        monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", CapturingOpenAIProvider)

        backend = build_backend(tmp_path / "workspace")
        try:
            by_model = {config.model: config.vision for config in CapturingOpenAIProvider.configs}
            assert by_model == {
                "catalog-text": False,
                "catalog-vision": True,
            }
        finally:
            await backend.close()
