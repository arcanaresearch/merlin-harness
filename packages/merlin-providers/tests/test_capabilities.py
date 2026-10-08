"""Tests for the normalized per-route model capability registry."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from merlin_providers.capabilities import (
    BUILTIN_CATALOG,
    CapabilityClaim,
    ModelCapabilityRegistry,
    OperatorOverride,
    RouteIdentity,
    SourceTier,
    litellm_catalogue,
    load_last_good_snapshot,
    load_snapshot_claims,
    openrouter_catalogue,
    overrides_from_provider_config,
    provider_native_catalogue,
)

ROUTE = RouteIdentity(
    transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
)


def _registry(**kwargs: object) -> ModelCapabilityRegistry:
    return ModelCapabilityRegistry(now=lambda: datetime(2026, 9, 4, tzinfo=UTC), **kwargs)  # type: ignore[arg-type]


def test_unknown_model_falls_back_to_labelled_conservative_assumption() -> None:
    registry = _registry()
    capabilities = registry.resolve(ROUTE)
    assert capabilities.context_window_tokens.value == 131_072
    assert capabilities.context_window_tokens.provenance.tier == SourceTier.CONSERVATIVE_ASSUMPTION
    assert capabilities.supports_reasoning.value is False
    assert capabilities.modalities.value == ("text",)
    for name in (
        "context_window_tokens",
        "max_output_tokens",
        "supports_tools",
        "supports_reasoning",
        "supports_streaming",
        "supports_prompt_cache",
        "supports_structured_output",
        "modalities",
    ):
        assert capabilities.field(name).provenance.tier == SourceTier.CONSERVATIVE_ASSUMPTION
        assert capabilities.field(name).provenance.confidence < 0.5


def test_builtin_catalogue_resolves_offline_with_prefix_matching() -> None:
    registry = _registry()
    route = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.openai.com/v1",
        model="gpt-5.4-heldout-v1",
    )
    capabilities = registry.resolve(route)
    assert capabilities.context_window_tokens.value == 400_000
    assert capabilities.context_window_tokens.provenance.tier == SourceTier.BUILTIN_REGISTRY
    assert capabilities.context_window_tokens.provenance.raw_model_id == "gpt-5.4-heldout-v1"
    assert capabilities.supports_reasoning.value is True
    for prefix in (
        "gpt-5",
        "claude-opus",
        "claude-sonnet",
        "deepseek-v4",
        "kimi-k2",
        "gemini-2.5-pro",
    ):
        assert prefix in BUILTIN_CATALOG


def test_operator_override_is_tier_one_and_route_scoped() -> None:
    override = OperatorOverride(route=ROUTE, values={"context_window_tokens": 32_768})
    registry = _registry(overrides=[override])
    capabilities = registry.resolve(ROUTE)
    assert capabilities.context_window_tokens.value == 32_768
    assert capabilities.context_window_tokens.provenance.tier == SourceTier.OPERATOR_OVERRIDE
    other = RouteIdentity(
        transport="openai_compatible", endpoint="https://other/v1", model="acme-1"
    )
    assert registry.resolve(other).context_window_tokens.value == 131_072


def test_live_metadata_beats_builtin_but_not_operator_override() -> None:
    class LiveHook:
        def capabilities(self, route: RouteIdentity) -> CapabilityClaim:
            return CapabilityClaim(
                tier=SourceTier.LIVE_ROUTE_METADATA,
                source="live",
                values={"context_window_tokens": 64_000},
            )

    route = RouteIdentity(transport="t", endpoint="e", model="gpt-5.4")
    override = OperatorOverride(route=route, values={"context_window_tokens": 48_000})
    live_only = _registry(live_hooks=[LiveHook()]).resolve(route)
    assert live_only.context_window_tokens.value == 64_000
    assert live_only.context_window_tokens.provenance.tier == SourceTier.LIVE_ROUTE_METADATA
    with_override = _registry(overrides=[override], live_hooks=[LiveHook()]).resolve(route)
    assert with_override.context_window_tokens.value == 48_000
    assert with_override.context_window_tokens.provenance.tier == SourceTier.OPERATOR_OVERRIDE


def test_conflict_prefers_route_specific_value_and_records_conflict() -> None:
    class LiveHook:
        def capabilities(self, route: RouteIdentity) -> CapabilityClaim:
            return CapabilityClaim(
                tier=SourceTier.LIVE_ROUTE_METADATA,
                source="live",
                values={"context_window_tokens": 64_000},
            )

    route = RouteIdentity(transport="t", endpoint="e", model="gpt-5.4")
    capabilities = _registry(live_hooks=[LiveHook()]).resolve(route)
    assert capabilities.context_window_tokens.value == 64_000
    fields = {conflict.field for conflict in capabilities.conflicts}
    assert "context_window_tokens" in fields
    assert "route-specific" in capabilities.context_window_tokens.provenance.conflicts[0]


def test_conflict_without_route_specific_source_uses_smallest_credible() -> None:
    class ProviderCatalogue:
        def lookup(self, route: RouteIdentity) -> CapabilityClaim:
            return CapabilityClaim(
                tier=SourceTier.PROVIDER_CATALOGUE,
                source="provider-catalogue",
                values={"context_window_tokens": 96_000},
            )

    route = RouteIdentity(transport="t", endpoint="e", model="acme-1")

    class Secondary:
        def lookup(self, route: RouteIdentity) -> CapabilityClaim:
            return CapabilityClaim(
                tier=SourceTier.SECONDARY_CATALOGUE,
                source="openrouter",
                values={"context_window_tokens": 200_000},
            )

    # Provider-native present: preferred over disagreeing secondary.
    native = _registry(catalogue_hooks=[ProviderCatalogue(), Secondary()]).resolve(route)
    assert native.context_window_tokens.value == 96_000

    # Builtin (128k via claim) vs secondary (200k), no route-specific/native:
    # smallest credible wins and the conflict is recorded.
    class BuiltinTier:
        def lookup(self, route: RouteIdentity) -> CapabilityClaim:
            return CapabilityClaim(
                tier=SourceTier.BUILTIN_REGISTRY,
                source="builtin:acme-1",
                values={"context_window_tokens": 128_000},
            )

    resolved = _registry(builtin_catalog={}, catalogue_hooks=[BuiltinTier(), Secondary()]).resolve(
        route
    )
    assert resolved.context_window_tokens.value == 128_000
    conflict = next(c for c in resolved.conflicts if c.field == "context_window_tokens")
    assert "smallest credible" in conflict.resolution
    assert ("openrouter", "200000") in conflict.values


def test_overflow_lowers_effective_limit_and_is_monotonic() -> None:
    registry = _registry()
    route = RouteIdentity(transport="t", endpoint="e", model="gpt-5.4")
    registry.record_context_overflow(route, 100_000)
    assert registry.resolve(route).context_window_tokens.value == 100_000
    # A larger later overflow observation must not raise the lowered limit.
    registry.record_context_overflow(route, 150_000)
    assert registry.resolve(route).context_window_tokens.value == 100_000
    registry.record_context_overflow(route, 90_000)
    assert registry.resolve(route).context_window_tokens.value == 90_000
    with pytest.raises(ValueError, match="positive"):
        registry.record_context_overflow(route, 0)


def test_successful_smaller_request_never_increases_advertised_maximum() -> None:
    registry = _registry()
    route = RouteIdentity(transport="t", endpoint="e", model="gpt-5.4")
    advertised = registry.resolve(route).context_window_tokens.value
    registry.observe_successful_request(route, 10_000)
    assert registry.resolve(route).context_window_tokens.value == advertised
    # Even after an overflow lowering, success only records a lower bound.
    registry.record_context_overflow(route, 50_000)
    registry.observe_successful_request(route, 80_000)
    assert registry.resolve(route).context_window_tokens.value == 50_000


def test_overrides_from_provider_config_consumes_existing_surfaces() -> None:
    provider = {
        "model": "acme-1",
        "base_url": "https://api.example/v1",
        "context_window_tokens": 65_536,
        "vision": True,
        "model_capabilities": {"acme-1": ["tools", "reasoning", "prompt_cache"]},
        "model_pricing": {
            "acme-1": {"input_cost_per_million": 1.0, "output_cost_per_million": 2.0}
        },
    }
    overrides = overrides_from_provider_config(provider)
    assert len(overrides) == 1
    override = overrides[0]
    assert override.route == RouteIdentity(
        transport="openai_compatible", endpoint="https://api.example/v1", model="acme-1"
    )
    assert override.values == {
        "context_window_tokens": 65_536,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_prompt_cache": True,
        "modalities": ("text", "image"),
        "input_price_per_token_usd": 1.0 / 1_000_000,
        "output_price_per_token_usd": 2.0 / 1_000_000,
    }
    assert overrides_from_provider_config({}) == ()


def test_snapshot_round_trips_known_routes(tmp_path: Path) -> None:
    override = OperatorOverride(route=ROUTE, values={"context_window_tokens": 32_768})
    registry = _registry(overrides=[override])
    path = ModelCapabilityRegistry.snapshot_path(tmp_path)
    registry.save_snapshot(path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["saved_at"].startswith("2026-09-04")
    (record,) = snapshot["records"]
    assert record["route"]["model"] == "acme-1"
    assert record["context_window_tokens"]["value"] == 32_768
    assert record["context_window_tokens"]["provenance"]["tier"] == 1


def test_secondary_catalogue_absence_is_not_evidence_of_nonsupport() -> None:
    class EmptyCatalogue:
        def lookup(self, route: RouteIdentity) -> CapabilityClaim | None:
            return None

    route = RouteIdentity(transport="t", endpoint="e", model="kimi-k2")
    capabilities = _registry(catalogue_hooks=[EmptyCatalogue()]).resolve(route)
    assert capabilities.supports_tools.value is True
    assert capabilities.context_window_tokens.value == 262_144


def test_snapshot_load_round_trip_restores_known_routes_offline(tmp_path: Path) -> None:
    override = OperatorOverride(route=ROUTE, values={"context_window_tokens": 32_768})
    path = ModelCapabilityRegistry.snapshot_path(tmp_path)
    _registry(overrides=[override]).save_snapshot(path)

    # Offline startup: no overrides, only the last-good snapshot as evidence.
    offline = _registry(snapshot_claims=load_snapshot_claims(path))
    capabilities = offline.resolve(ROUTE)
    assert capabilities.context_window_tokens.value == 32_768
    assert capabilities.context_window_tokens.provenance.tier == SourceTier.OPERATOR_OVERRIDE
    # Tier-7 assumptions were never cached: unevidenced fields stay labelled.
    assert capabilities.supports_reasoning.provenance.assumed


def test_snapshot_round_trip_is_the_startup_path(tmp_path: Path) -> None:
    # The bootstrap flow: overrides from config + last-good snapshot, then a
    # refreshed snapshot is saved for the next offline start.
    path = ModelCapabilityRegistry.snapshot_path(tmp_path)
    override = OperatorOverride(route=ROUTE, values={"context_window_tokens": 32_768})
    _registry(overrides=[override]).save_snapshot(path)

    registry = ModelCapabilityRegistry(
        overrides=(),
        snapshot_claims=load_last_good_snapshot(path),
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert registry.resolve(ROUTE).context_window_tokens.value == 32_768
    registry.add_override(override)
    registry.save_snapshot(path)
    assert load_last_good_snapshot(path)[ROUTE]


def test_load_last_good_snapshot_tolerates_missing_and_corrupt_cache(tmp_path: Path) -> None:
    path = ModelCapabilityRegistry.snapshot_path(tmp_path)
    assert load_last_good_snapshot(path) == {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_last_good_snapshot(path) == {}
    path.write_text(json.dumps({"records": [{"route": {"model": 1}}]}), encoding="utf-8")
    assert load_last_good_snapshot(path) == {}


def test_resolve_model_is_the_duck_typed_consumer_seam() -> None:
    registry = _registry()
    known = registry.resolve_model("gpt-5.4-heldout-v1")
    assert known.context_window_tokens.value == 400_000
    assert not known.context_window_tokens.provenance.assumed
    unknown = registry.resolve_model("mystery-model-1")
    assert unknown.context_window_tokens.value == 131_072
    assert unknown.context_window_tokens.provenance.assumed


def test_same_model_on_two_transports_resolves_per_route() -> None:
    overrides = (
        OperatorOverride(
            route=RouteIdentity(
                transport="openai_compatible",
                endpoint="https://api.example/v1",
                model="acme-1",
            ),
            values={"context_window_tokens": 64_000},
        ),
        OperatorOverride(
            route=RouteIdentity(transport="claude_cli", endpoint="", model="acme-1"),
            values={"context_window_tokens": 16_000},
        ),
    )
    registry = _registry(overrides=overrides)
    via_http = registry.resolve_model(
        "acme-1", transport="openai_compatible", endpoint="https://api.example/v1"
    )
    via_cli = registry.resolve_model("acme-1", transport="claude_cli", endpoint="")
    assert via_http.context_window_tokens.value == 64_000
    assert via_cli.context_window_tokens.value == 16_000


def test_per_route_override_wins_over_builtin_catalog() -> None:
    override = OperatorOverride(
        route=RouteIdentity(transport="gemini", endpoint="https://gemini.example", model="gpt-5.4"),
        values={"context_window_tokens": 90_000},
    )
    registry = _registry(overrides=[override])
    resolved = registry.resolve_model(
        "gpt-5.4", transport="gemini", endpoint="https://gemini.example"
    )
    assert resolved.context_window_tokens.value == 90_000
    assert resolved.context_window_tokens.provenance.tier is SourceTier.OPERATOR_OVERRIDE
    # The same model on another route still gets the built-in family entry.
    other = registry.resolve_model("gpt-5.4", transport="openai_compatible", endpoint="")
    assert other.context_window_tokens.value == 400_000
    assert other.context_window_tokens.provenance.tier is SourceTier.BUILTIN_REGISTRY


def test_stub_catalogue_hooks_contribute_no_claims_and_tier_seven_stays_visible() -> None:
    # Tiers 4-6 are unimplemented placeholders: routing an unknown model through
    # them must fall through to the explicitly labelled tier-7 assumption.
    registry = _registry(
        catalogue_hooks=[
            provider_native_catalogue("acme"),
            openrouter_catalogue(),
            litellm_catalogue(),
        ]
    )
    capabilities = registry.resolve(ROUTE)
    assert capabilities.context_window_tokens.value == 131_072
    assert capabilities.max_output_tokens.value == 4_096
    for name in (
        "context_window_tokens",
        "max_output_tokens",
        "supports_tools",
        "supports_reasoning",
        "supports_streaming",
        "supports_prompt_cache",
        "supports_structured_output",
        "modalities",
    ):
        provenance = capabilities.field(name).provenance
        assert provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION
        assert provenance.assumed
        assert provenance.confidence == 0.2


def test_operator_context_override_above_catalog_value_warns_without_clamping() -> None:
    override = OperatorOverride(
        route=RouteIdentity(transport="t", endpoint="e", model="gpt-5.4"),
        values={"context_window_tokens": 900_000},
    )
    resolved = _registry(overrides=[override]).resolve_model("gpt-5.4", transport="t", endpoint="e")
    field = resolved.context_window_tokens
    # Warning-only: the operator value stands, provenance flags the excess.
    assert field.value == 900_000
    assert field.provenance.tier is SourceTier.OPERATOR_OVERRIDE
    assert any(
        "exceeds the known catalog value 400000" in note for note in field.provenance.conflicts
    )


def test_operator_context_override_above_hard_ceiling_warns_without_clamping() -> None:
    override = OperatorOverride(route=ROUTE, values={"context_window_tokens": 9_999_999})
    resolved = _registry(overrides=[override]).resolve(ROUTE)
    field = resolved.context_window_tokens
    assert field.value == 9_999_999
    assert any("exceeds the sanity ceiling" in note for note in field.provenance.conflicts)


def test_operator_context_override_within_known_bounds_stays_quiet() -> None:
    override = OperatorOverride(
        route=RouteIdentity(transport="t", endpoint="e", model="gpt-5.4"),
        values={"context_window_tokens": 128_000},
    )
    resolved = _registry(overrides=[override]).resolve_model("gpt-5.4", transport="t", endpoint="e")
    assert not any(
        "exceeds" in note for note in resolved.context_window_tokens.provenance.conflicts
    )
    # An unknown model with a plausible window is not flagged either.
    quiet = OperatorOverride(route=ROUTE, values={"context_window_tokens": 200_000})
    assert (
        _registry(overrides=[quiet]).resolve(ROUTE).context_window_tokens.provenance.conflicts == ()
    )


def test_overrides_from_provider_config_scopes_to_each_configured_route() -> None:
    provider = {
        "model": "acme-1",
        "base_url": "https://api.example/v1",
        "context_window_tokens": 65_536,
    }
    (cli,) = overrides_from_provider_config(provider, transport="claude_cli", endpoint="")
    assert cli.route == RouteIdentity(transport="claude_cli", endpoint="", model="acme-1")
    (native,) = overrides_from_provider_config(provider, transport="anthropic_messages")
    assert native.route == RouteIdentity(
        transport="anthropic_messages", endpoint="https://api.example/v1", model="acme-1"
    )


# --- OpenRouter tier-5 catalogue hook -------------------------------------

from collections.abc import Callable

import httpx

OPENROUTER_ROUTE = RouteIdentity(
    transport="openai_compatible",
    endpoint="https://openrouter.ai/api/v1",
    model="acme/visionary-9",
)


def _openrouter_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "acme/visionary-9",
                "context_length": 256_000,
                "top_provider": {"max_completion_tokens": 32_000},
                "architecture": {"input_modalities": ["text", "image"]},
                "pricing": {"prompt": "0.000001", "completion": "0.000004"},
            },
            {
                "id": "acme/plain-1",
                "context_length": 65_536,
                "architecture": {"input_modalities": ["text"]},
            },
        ]
    }


def _openrouter_hook(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tmp_path: Path,
    ttl: float = 600.0,
    clock: Callable[[], float] | None = None,
    **kwargs: object,
):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return openrouter_catalogue(
        client=client,
        ttl_seconds=ttl,
        cache_path=tmp_path / "openrouter-models.json",
        clock=clock or (lambda: 0.0),
        **kwargs,  # type: ignore[arg-type]
    )


def test_openrouter_hit_resolves_context_output_and_modalities(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_openrouter_payload())

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    claim = hook.lookup(OPENROUTER_ROUTE)
    assert claim is not None
    assert claim.tier is SourceTier.SECONDARY_CATALOGUE
    assert claim.source == "openrouter"
    assert claim.values["context_window_tokens"] == 256_000
    assert claim.values["max_output_tokens"] == 32_000
    assert claim.values["modalities"] == ("text", "image")
    # OpenRouter pricing.prompt/completion are already USD per token strings.
    assert claim.values["input_price_per_token_usd"] == pytest.approx(0.000001)
    assert claim.values["output_price_per_token_usd"] == pytest.approx(0.000004)
    assert claim.raw_model_id == "acme/visionary-9"
    assert len(calls) == 1


def test_pricing_without_any_source_is_unknown_not_invented(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openrouter_payload())

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    resolved = _registry(catalogue_hooks=[hook]).resolve(
        RouteIdentity(transport="t", endpoint="https://openrouter.ai/api/v1", model="acme/plain-1")
    )
    # acme/plain-1 carries no pricing block: unknown price stays None and is
    # labelled as an assumption, never presented as catalogue-backed.
    assert resolved.input_price_per_token_usd.value is None
    assert resolved.input_price_per_token_usd.provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION
    assert resolved.input_price_per_token_usd.provenance.assumed
    assert resolved.output_price_per_token_usd.value is None


def test_operator_pricing_override_beats_catalogue(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openrouter_payload())

    registry = _registry(
        overrides=overrides_from_provider_config(
            {
                "model": "acme/visionary-9",
                "base_url": "https://openrouter.ai/api/v1",
                "model_pricing": {
                    "acme/visionary-9": {
                        "input_cost_per_million": 2.0,
                        "output_cost_per_million": 8.0,
                    }
                },
            }
        ),
        catalogue_hooks=[_openrouter_hook(handler, tmp_path=tmp_path)],
    )
    resolved = registry.resolve(OPENROUTER_ROUTE)
    assert resolved.input_price_per_token_usd.value == pytest.approx(2.0 / 1_000_000)
    assert resolved.input_price_per_token_usd.provenance.tier is SourceTier.OPERATOR_OVERRIDE
    assert resolved.output_price_per_token_usd.value == pytest.approx(8.0 / 1_000_000)


def test_openrouter_claims_flow_through_registry_at_tier_five(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openrouter_payload())

    registry = _registry(catalogue_hooks=[_openrouter_hook(handler, tmp_path=tmp_path)])
    resolved = registry.resolve(OPENROUTER_ROUTE)
    assert resolved.context_window_tokens.value == 256_000
    assert resolved.context_window_tokens.provenance.tier is SourceTier.SECONDARY_CATALOGUE
    assert resolved.context_window_tokens.provenance.confidence == 0.6
    # Absence from the catalogue is not evidence of nonsupport: boolean fields
    # still fall through to the labelled tier-7 assumption.
    assert resolved.supports_reasoning.provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION


def test_openrouter_miss_returns_none_and_stays_assumed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openrouter_payload())

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    missing = hook.lookup(
        RouteIdentity(transport="t", endpoint="https://openrouter.ai/api/v1", model="nope/none")
    )
    assert missing is None


def test_openrouter_never_fetches_for_unrelated_endpoints(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_openrouter_payload())

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    # Default gating: an unrelated endpoint with a vendor/model slug stays offline.
    assert (
        hook.lookup(RouteIdentity(transport="t", endpoint="https://api.example/v1", model="a/b"))
        is None
    )
    assert calls == []
    # Explicit opt-in enables the vendor-slug heuristic.
    opted_in = _openrouter_hook(handler, tmp_path=tmp_path, match_vendor_slug=True)
    assert (
        opted_in.lookup(
            RouteIdentity(transport="t", endpoint="https://api.example/v1", model="acme/plain-1")
        )
        is not None
    )
    assert len(calls) == 1


def test_openrouter_ttl_cache_fetches_once_per_window(tmp_path: Path) -> None:
    calls = []
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_openrouter_payload())

    hook = _openrouter_hook(handler, tmp_path=tmp_path, ttl=60.0, clock=lambda: now[0])
    assert hook.lookup(OPENROUTER_ROUTE) is not None
    hook.lookup(OPENROUTER_ROUTE)
    assert len(calls) == 1
    now[0] = 61.0  # past the TTL: the next lookup refetches
    hook.lookup(OPENROUTER_ROUTE)
    assert len(calls) == 2


def test_openrouter_timeout_falls_through_silently(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    assert hook.lookup(OPENROUTER_ROUTE) is None
    registry = _registry(catalogue_hooks=[hook])
    resolved = registry.resolve(OPENROUTER_ROUTE)
    assert resolved.context_window_tokens.provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION


def test_openrouter_http_error_status_falls_through(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    assert hook.lookup(OPENROUTER_ROUTE) is None


def test_openrouter_stale_memory_serves_when_refresh_fails(tmp_path: Path) -> None:
    calls = []
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=_openrouter_payload())
        raise httpx.ConnectError("offline", request=request)

    hook = _openrouter_hook(handler, tmp_path=tmp_path, ttl=10.0, clock=lambda: now[0])
    assert hook.lookup(OPENROUTER_ROUTE) is not None
    now[0] = 20.0  # TTL expired; refresh fails but the stale copy still answers
    claim = hook.lookup(OPENROUTER_ROUTE)
    assert claim is not None
    assert claim.values["context_window_tokens"] == 256_000


def test_openrouter_last_good_file_is_the_offline_fallback(tmp_path: Path) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openrouter_payload())

    cache_path = tmp_path / "openrouter-models.json"
    warm = _openrouter_hook(ok, tmp_path=tmp_path)
    assert warm.lookup(OPENROUTER_ROUTE) is not None
    assert cache_path.exists()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    cold = _openrouter_hook(offline, tmp_path=tmp_path)
    claim = cold.lookup(OPENROUTER_ROUTE)
    assert claim is not None
    assert claim.tier is SourceTier.SECONDARY_CATALOGUE
    assert claim.values["context_window_tokens"] == 256_000


def test_openrouter_malformed_payload_falls_through(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    hook = _openrouter_hook(handler, tmp_path=tmp_path)
    assert hook.lookup(OPENROUTER_ROUTE) is None


# --- models.dev tier-5 catalogue hook ---------------------------------------

from merlin_providers.capabilities import models_dev_catalogue

MODELS_DEV_ROUTE = RouteIdentity(
    transport="openai_compatible",
    endpoint="https://api.deepseek.com/v1",
    model="deepseek-reasoner-x",
)


def _models_dev_payload() -> dict[str, object]:
    return {
        "deepseek": {
            "id": "deepseek",
            "name": "DeepSeek",
            "models": {
                "deepseek-reasoner-x": {
                    "id": "deepseek-reasoner-x",
                    "limit": {"context": 131_072, "output": 65_536},
                    "cost": {"input": 0.55, "output": 2.19, "cache_read": 0.14},
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "reasoning": True,
                    "tool_call": True,
                },
                "deepseek-v9-flash": {
                    "id": "deepseek-v9-flash",
                    "limit": {"context": 262_144, "output": 8_192},
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "reasoning": False,
                    "tool_call": True,
                },
            },
        },
        "openai": {
            "id": "openai",
            "models": {
                "gpt-9-mini": {
                    "id": "gpt-9-mini",
                    "limit": {"context": 128_000, "output": 16_384},
                    "tool_call": True,
                }
            },
        },
    }


def _models_dev_hook(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tmp_path: Path,
    ttl: float = 600.0,
    clock: Callable[[], float] | None = None,
    **kwargs: object,
):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return models_dev_catalogue(
        client=client,
        ttl_seconds=ttl,
        cache_path=tmp_path / "models-dev-api.json",
        clock=clock or (lambda: 0.0),
        **kwargs,  # type: ignore[arg-type]
    )


def test_models_dev_hit_resolves_context_output_reasoning_and_tools(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_models_dev_payload())

    hook = _models_dev_hook(handler, tmp_path=tmp_path)
    claim = hook.lookup(MODELS_DEV_ROUTE)
    assert claim is not None
    assert claim.tier is SourceTier.SECONDARY_CATALOGUE
    assert claim.source == "models.dev"
    assert claim.values["context_window_tokens"] == 131_072
    assert claim.values["max_output_tokens"] == 65_536
    assert claim.values["supports_reasoning"] is True
    assert claim.values["supports_tools"] is True
    assert claim.values["modalities"] == ("text",)
    # models.dev cost.input/output are USD per million tokens; the claim
    # carries them as per-token pricing fields.
    assert claim.values["input_price_per_token_usd"] == pytest.approx(0.55 / 1_000_000)
    assert claim.values["output_price_per_token_usd"] == pytest.approx(2.19 / 1_000_000)

    registry = _registry(catalogue_hooks=[hook])
    resolved = registry.resolve(MODELS_DEV_ROUTE)
    assert resolved.context_window_tokens.value == 131_072
    assert resolved.context_window_tokens.provenance.tier is SourceTier.SECONDARY_CATALOGUE
    assert resolved.context_window_tokens.provenance.confidence == 0.6
    assert resolved.supports_reasoning.value is True
    assert resolved.supports_reasoning.provenance.tier is SourceTier.SECONDARY_CATALOGUE
    assert resolved.input_price_per_token_usd.value == pytest.approx(0.55 / 1_000_000)
    assert resolved.input_price_per_token_usd.provenance.tier is SourceTier.SECONDARY_CATALOGUE
    assert resolved.output_price_per_token_usd.value == pytest.approx(2.19 / 1_000_000)
    # Absence from the catalogue is not evidence: unclaimed fields stay assumed.
    assert resolved.supports_streaming.provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION


def test_models_dev_prefix_match_resolves_dated_variants(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_models_dev_payload())

    hook = _models_dev_hook(handler, tmp_path=tmp_path)
    route = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.deepseek.com/v1",
        model="deepseek-v9-flash-0901",
    )
    claim = hook.lookup(route)
    assert claim is not None
    assert claim.values["context_window_tokens"] == 262_144
    assert claim.values["modalities"] == ("text", "image")


def test_models_dev_builtin_registry_still_wins(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _models_dev_payload()
        payload["deepseek"]["models"]["deepseek-v4-pro"] = {  # type: ignore[index]
            "id": "deepseek-v4-pro",
            # Larger than the reviewed builtin window: smallest-credible keeps
            # the builtin value, and tier 3 still owns the provenance.
            "limit": {"context": 2_000_000, "output": 1_024},
        }
        return httpx.Response(200, json=payload)

    registry = _registry(catalogue_hooks=[_models_dev_hook(handler, tmp_path=tmp_path)])
    route = RouteIdentity(
        transport="openai_compatible",
        endpoint="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
    )
    resolved = registry.resolve(route)
    assert resolved.context_window_tokens.value == 1_048_576
    assert resolved.context_window_tokens.provenance.tier is SourceTier.BUILTIN_REGISTRY
    assert resolved.supports_reasoning.value is True
    assert resolved.supports_reasoning.provenance.tier is SourceTier.BUILTIN_REGISTRY


def test_models_dev_unknown_host_never_fetches(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_models_dev_payload())

    hook = _models_dev_hook(handler, tmp_path=tmp_path)
    assert (
        hook.lookup(
            RouteIdentity(transport="t", endpoint="https://api.example.com/v1", model="gpt-9-mini")
        )
        is None
    )
    assert calls == []


def test_models_dev_last_good_file_is_the_offline_fallback(tmp_path: Path) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_models_dev_payload())

    cache_path = tmp_path / "models-dev-api.json"
    warm = _models_dev_hook(ok, tmp_path=tmp_path)
    assert warm.lookup(MODELS_DEV_ROUTE) is not None
    assert cache_path.exists()

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    cold = _models_dev_hook(offline, tmp_path=tmp_path)
    claim = cold.lookup(MODELS_DEV_ROUTE)
    assert claim is not None
    assert claim.tier is SourceTier.SECONDARY_CATALOGUE
    assert claim.values["context_window_tokens"] == 131_072


def test_models_dev_malformed_or_oversized_payload_falls_through(tmp_path: Path) -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "mapping"])

    hook = _models_dev_hook(malformed, tmp_path=tmp_path)
    assert hook.lookup(MODELS_DEV_ROUTE) is None
    resolved = _registry(catalogue_hooks=[hook]).resolve(MODELS_DEV_ROUTE)
    assert resolved.context_window_tokens.provenance.tier is SourceTier.CONSERVATIVE_ASSUMPTION

    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_models_dev_payload())

    capped = _models_dev_hook(oversized, tmp_path=tmp_path, max_bytes=64)
    assert capped.lookup(MODELS_DEV_ROUTE) is None


def test_deepseek_v4_flash_and_pro_catalogue_windows() -> None:
    """OpenRouter frontend catalog (fetched 2026-09-05): v4 flash/pro = 1M ctx."""
    registry = _registry()
    for model in ("deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"):
        route = RouteIdentity(
            transport="openai_compatible",
            endpoint="https://api.deepseek.com/v1",
            model=model,
        )
        resolved = registry.resolve(route)
        assert resolved.context_window_tokens.value == 1_048_576, model
        assert resolved.context_window_tokens.provenance.tier == SourceTier.BUILTIN_REGISTRY
        assert resolved.context_window_tokens.provenance.raw_model_id == model
