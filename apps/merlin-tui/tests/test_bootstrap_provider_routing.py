from pathlib import Path

import pytest
from merlin_tui.bootstrap import (
    _capability_values_for_route,
    _route_cache_read_pricing_entries,
    _route_vision_enabled,
)


def test_operator_pricing_without_cache_suppresses_catalog_discount() -> None:
    entries = _route_cache_read_pricing_entries(
        {"operator-priced", "catalog-priced"},
        {"operator-priced": (7.0, 8.0)},
        {"operator-priced": None},
        lambda _model: (1e-6, 2e-6, 0.25e-6),
    )

    assert dict(entries) == {
        "catalog-priced": 0.25,
        "operator-priced": None,
    }


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

    for transport in (
        "gemini",
        "codex_app_server",
        "claude_cli",
        "kimi_acp",
        "kiro_acp",
        "opencode_acp",
        "pi_rpc",
    ):
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


@pytest.mark.asyncio
async def test_build_backend_constructs_generic_acp_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from merlin_config import Config, CredentialStore
    from merlin_storage_postgres import Database, DatabaseConfig
    from merlin_tui.bootstrap import build_backend

    class CapturingAcpProvider:
        configs = []

        def __init__(self, config) -> None:
            self.config = config
            type(self).configs.append(config)

        async def close(self) -> None:
            return None

    CapturingAcpProvider.configs = []

    class CapturingPiProvider(CapturingAcpProvider):
        configs = []

    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "https://api.example/v1",
                        "model": "default-model",
                        "model_transports": {
                            "kiro-model": "kiro_acp",
                            "opencode-model": "opencode_acp",
                            "pi-model": "pi_rpc",
                        },
                        "model_efforts": {"kiro-model": "high"},
                    }
                },
            ),
        )
        monkeypatch.setattr(CredentialStore, "load", lambda _self: {"api_key": "secret"})
        monkeypatch.setattr("merlin_providers.KimiAcpProvider", CapturingAcpProvider)
        monkeypatch.setattr("merlin_providers.PiRpcProvider", CapturingPiProvider)

        backend = build_backend(tmp_path / "workspace")
        try:
            configs = {config.transport: config for config in CapturingAcpProvider.configs}
            assert configs["kiro_acp"].command == (
                "kiro-cli",
                "acp",
                "--model",
                "kiro-model",
                "--effort",
                "high",
            )
            assert configs["kiro_acp"].mode_id is None
            assert configs["kiro_acp"].configure_model_in_session is False
            assert configs["opencode_acp"].command == ("opencode", "acp")
            assert configs["opencode_acp"].mode_id is None
            assert CapturingPiProvider.configs[0].model == "pi-model"
            assert CapturingPiProvider.configs[0].reasoning_effort == "default"
        finally:
            await backend.close()


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


def test_exact_vision_denial_beats_catalog_without_leaking_to_other_models() -> None:
    from merlin_providers import ModelCapabilityRegistry

    registry = ModelCapabilityRegistry(
        builtin_catalog={
            "catalog-denied": {"modalities": ("text", "image")},
            "catalog-discovered": {"modalities": ("text", "image")},
            "catalog-operator-enabled": {"modalities": ("text",)},
        }
    )
    provider = {
        "context_window_tokens": 65_536,
        "input_cost_per_million": 9.0,
        "output_cost_per_million": 12.0,
        "model_capabilities": {
            "catalog-denied": [],
            "catalog-operator-enabled": ["vision"],
        },
    }

    assert not _route_vision_enabled(
        provider,
        "catalog-denied",
        "openai_compatible",
        default_model="default",
        capability_registry=registry,
        endpoint="https://api.example/v1",
    )
    assert _route_vision_enabled(
        provider,
        "catalog-discovered",
        "openai_compatible",
        default_model="default",
        capability_registry=registry,
        endpoint="https://api.example/v1",
    )
    assert _route_vision_enabled(
        provider,
        "catalog-operator-enabled",
        "openai_compatible",
        default_model="default",
        capability_registry=registry,
        endpoint="https://api.example/v1",
    )

    denied = _capability_values_for_route(
        provider,
        "catalog-denied",
        "openai_compatible",
        default_model="default",
    )
    assert denied["model_capabilities"] == {"catalog-denied": []}
    assert denied["vision"] is False
    for default_only in (
        "context_window_tokens",
        "input_cost_per_million",
        "output_cost_per_million",
    ):
        assert default_only not in denied

    discovered = _capability_values_for_route(
        provider,
        "catalog-discovered",
        "openai_compatible",
        default_model="default",
    )
    assert "model_capabilities" not in discovered
    assert "vision" not in discovered

    legacy_false = _capability_values_for_route(
        {"vision": False},
        "catalog-discovered",
        "openai_compatible",
        default_model="catalog-discovered",
    )
    assert legacy_false["vision"] is False
    assert not _route_vision_enabled(
        legacy_false,
        "catalog-discovered",
        "openai_compatible",
        default_model="catalog-discovered",
        capability_registry=registry,
        endpoint="https://api.example/v1",
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
        monkeypatch.setattr("merlin_providers.OpenAICompatibleProvider", CapturingOpenAIProvider)
        monkeypatch.setattr("merlin_providers.OpenAIResponsesProvider", CapturingResponsesProvider)
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
    from merlin_providers import (
        CapabilityClaim,
        ProviderCapabilities,
        RouteIdentity,
        SourceTier,
    )
    from merlin_storage_postgres import Database, DatabaseConfig
    from merlin_tui.bootstrap import build_backend

    class Catalog:
        def lookup(self, route):
            metadata = {
                "catalog-text": (65_536, ("text",), 20.0, 30.0, 10.0),
                "catalog-denied": (131_072, ("text", "image"), 1.0, 2.0, 0.5),
                "catalog-escalation-vision": (
                    262_144,
                    ("text", "image"),
                    3.0,
                    4.0,
                    1.5,
                ),
                "catalog-escalation-text": (524_288, ("text",), 5.0, 6.0, 2.5),
            }
            if route.model not in metadata:
                return None
            context, modalities, input_cost, output_cost, cache_read_cost = metadata[route.model]
            return CapabilityClaim(
                tier=SourceTier.SECONDARY_CATALOGUE,
                source="models.dev",
                values={
                    "context_window_tokens": context,
                    "modalities": modalities,
                    "input_price_per_token_usd": input_cost / 1_000_000,
                    "output_price_per_token_usd": output_cost / 1_000_000,
                    "cache_read_input_price_per_token_usd": cache_read_cost / 1_000_000,
                },
                retrieved_at="2026-10-28T00:00:00+00:00",
                raw_model_id=route.model,
            )

    class CapturingOpenAIProvider:
        configs = []
        instances = []
        transport = "openai_compatible"

        def __init__(self, config) -> None:
            self.config = config
            self.close_calls = 0
            self.capabilities = ProviderCapabilities(
                model=config.model,
                vision=config.vision,
                context_window_tokens=config.context_window_tokens,
            )
            self.route_identity = RouteIdentity(
                transport=self.transport,
                endpoint=config.base_url,
                model=config.model,
            )
            type(self).configs.append(config)
            type(self).instances.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    class CapturingResponsesProvider(CapturingOpenAIProvider):
        configs = []
        instances = []
        transport = "openai_responses"

    CapturingOpenAIProvider.configs = []
    CapturingOpenAIProvider.instances = []
    CapturingResponsesProvider.configs = []
    CapturingResponsesProvider.instances = []
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        monkeypatch.setattr(
            "merlin_config.load_config",
            lambda: Config(
                database_url=database.url,
                values={
                    "provider": {
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "catalog-text",
                        "context_window_tokens": 32_768,
                        "input_cost_per_million": 9.0,
                        "output_cost_per_million": 12.0,
                        "cache_read_input_cost_per_million": 4.0,
                        "role_models": {"recon": "catalog-denied"},
                        "model_capabilities": {"catalog-denied": []},
                        "model_pricing": {
                            "catalog-escalation-vision": {
                                "input_cost_per_million": 7.0,
                                "output_cost_per_million": 8.0,
                            }
                        },
                        "escalation_models": {
                            "recon": [
                                "catalog-denied",
                                "catalog-escalation-vision",
                                "catalog-escalation-text",
                                "catalog-unknown",
                            ]
                        },
                        "model_transports": {
                            "catalog-escalation-vision": "openai_responses",
                        },
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
        monkeypatch.setattr("merlin_providers.OpenAIResponsesProvider", CapturingResponsesProvider)

        backend = build_backend(tmp_path / "workspace")
        try:
            by_model = {config.model: config for config in CapturingOpenAIProvider.configs}
            assert set(by_model) == {
                "catalog-text",
                "catalog-denied",
                "catalog-escalation-text",
                "catalog-unknown",
            }
            assert (
                by_model["catalog-text"].vision,
                by_model["catalog-text"].context_window_tokens,
                by_model["catalog-text"].input_cost_per_million,
                by_model["catalog-text"].output_cost_per_million,
                by_model["catalog-text"].cache_read_input_cost_per_million,
            ) == (False, 32_768, 9.0, 12.0, 4.0)
            assert (
                by_model["catalog-denied"].vision,
                by_model["catalog-denied"].context_window_tokens,
                by_model["catalog-denied"].input_cost_per_million,
                by_model["catalog-denied"].output_cost_per_million,
                by_model["catalog-denied"].cache_read_input_cost_per_million,
            ) == (False, 131_072, 1.0, 2.0, 0.5)
            assert (
                by_model["catalog-escalation-text"].vision,
                by_model["catalog-escalation-text"].context_window_tokens,
                by_model["catalog-escalation-text"].input_cost_per_million,
                by_model["catalog-escalation-text"].output_cost_per_million,
                by_model["catalog-escalation-text"].cache_read_input_cost_per_million,
            ) == (False, 524_288, 5.0, 6.0, 2.5)
            assert (
                by_model["catalog-unknown"].vision,
                by_model["catalog-unknown"].context_window_tokens,
                by_model["catalog-unknown"].input_cost_per_million,
                by_model["catalog-unknown"].output_cost_per_million,
                by_model["catalog-unknown"].cache_read_input_cost_per_million,
            ) == (False, None, None, None, None)
            (responses_config,) = CapturingResponsesProvider.configs
            assert (
                responses_config.model,
                responses_config.vision,
                responses_config.context_window_tokens,
                responses_config.input_cost_per_million,
                responses_config.output_cost_per_million,
                responses_config.cache_read_input_cost_per_million,
            ) == ("catalog-escalation-vision", True, 262_144, 7.0, 8.0, None)

            routing = backend.kernel._provider  # noqa: SLF001
            assert {
                routed_model: (
                    routing.capabilities_for_model(routed_model).vision,
                    routing.capabilities_for_model(routed_model).context_window_tokens,
                )
                for routed_model in (
                    "catalog-text",
                    "catalog-denied",
                    "catalog-escalation-vision",
                    "catalog-escalation-text",
                    "catalog-unknown",
                )
            } == {
                "catalog-text": (False, 32_768),
                "catalog-denied": (False, 131_072),
                "catalog-escalation-vision": (True, 262_144),
                "catalog-escalation-text": (False, 524_288),
                "catalog-unknown": (False, None),
            }
            await backend.kernel._provider.close()  # noqa: SLF001
        finally:
            await backend.close()
        instances = CapturingOpenAIProvider.instances + CapturingResponsesProvider.instances
        assert len(instances) == 5
        assert [provider.close_calls for provider in instances] == [1, 1, 1, 1, 1]
