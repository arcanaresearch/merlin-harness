"""Normalized per-route model capability registry.

Resolves context, output, protocol, tool, schema, reasoning, media, streaming,
and prompt-cache capabilities for an exact routed (transport, endpoint, model)
triple. Each field is resolved independently in this tier order:

1. operator override scoped to the exact transport + endpoint + model;
2. live route/provider metadata, when the transport exposes it;
3. Merlin's refreshed, reviewed built-in registry;
4. provider-native public catalogues — UNIMPLEMENTED placeholder hook;
5. public secondary catalogues — OpenRouter (implemented, gated to
   ``openrouter.ai`` endpoints by default), models.dev (implemented, gated
   to known provider API hosts), LiteLLM (placeholder hook);
6. runtime/model metadata — Ollama, vLLM, llama.cpp, Hugging Face — UNIMPLEMENTED placeholder hook;
7. an explicitly labelled conservative assumption.

Tiers 4 and 6 (and LiteLLM in tier 5) ship as placeholder hooks that always
return no claims, so any model absent from tiers 1-3 and the OpenRouter
catalogue resolves at tier 7 with ``assumed`` provenance and low
confidence — never silently presented as catalogue-backed.

The registry consumes the existing ``provider.model_capabilities``,
``provider.model_pricing``, and per-provider ``context_window_tokens``
configuration surfaces as tier-1 operator overrides via
:func:`overrides_from_provider_config`; it does not maintain a parallel source
of truth.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

__all__ = [
    "BUILTIN_CATALOG",
    "CapabilityClaim",
    "CapabilityConflict",
    "CatalogueHook",
    "FieldProvenance",
    "LiveMetadataHook",
    "ModelCapabilities",
    "ModelCapabilityRegistry",
    "OperatorOverride",
    "ResolvedField",
    "RouteIdentity",
    "RuntimeMetadataHook",
    "SourceTier",
    "load_last_good_snapshot",
    "load_snapshot_claims",
    "overrides_from_provider_config",
]


class SourceTier(IntEnum):
    OPERATOR_OVERRIDE = 1
    LIVE_ROUTE_METADATA = 2
    BUILTIN_REGISTRY = 3
    PROVIDER_CATALOGUE = 4
    SECONDARY_CATALOGUE = 5
    RUNTIME_METADATA = 6
    CONSERVATIVE_ASSUMPTION = 7


_CAPABILITY_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    "supports_tools",
    "supports_reasoning",
    "supports_streaming",
    "supports_prompt_cache",
    "supports_structured_output",
    "modalities",
    "input_price_per_token_usd",
    "output_price_per_token_usd",
    "cache_read_input_price_per_token_usd",
)

_TIER_CONFIDENCE = {
    SourceTier.OPERATOR_OVERRIDE: 1.0,
    SourceTier.LIVE_ROUTE_METADATA: 0.95,
    SourceTier.BUILTIN_REGISTRY: 0.9,
    SourceTier.PROVIDER_CATALOGUE: 0.8,
    SourceTier.SECONDARY_CATALOGUE: 0.6,
    SourceTier.RUNTIME_METADATA: 0.5,
    SourceTier.CONSERVATIVE_ASSUMPTION: 0.2,
}

# Tier-7 conservative assumptions. Deliberately small: absence of evidence must
# never inflate a budget. The context fallback matches the runtime
# ``agent_context_fallback_tokens`` default in merlin-config.
_ASSUMPTIONS: dict[str, object] = {
    "context_window_tokens": 131_072,
    "max_output_tokens": 4_096,
    "supports_tools": True,
    "supports_reasoning": False,
    "supports_streaming": True,
    "supports_prompt_cache": False,
    "supports_structured_output": False,
    "modalities": ("text",),
    # Pricing has no credible assumption: unknown means unknown (None), never
    # an invented rate.
    "input_price_per_token_usd": None,
    "output_price_per_token_usd": None,
    "cache_read_input_price_per_token_usd": None,
}

# Operator-override sanity ceiling for context_window_tokens. Generous — well
# above every built-in catalogue value — and warning-only: operators may
# legitimately test huge-window models, but an absurd override must be visible
# in provenance rather than silently trusted.
_OPERATOR_CONTEXT_HARD_CEILING = 4_000_000


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    """Exact routed identity: a model ID alone is not a capability identity."""

    transport: str
    endpoint: str
    model: str


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    tier: SourceTier
    retrieved_at: str | None
    raw_model_id: str
    transport: str
    endpoint: str
    confidence: float
    corroborating_sources: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @property
    def assumed(self) -> bool:
        """True only when the value is the tier-7 conservative assumption."""
        return self.tier is SourceTier.CONSERVATIVE_ASSUMPTION


@dataclass(frozen=True, slots=True)
class CapabilityConflict:
    field: str
    values: tuple[tuple[str, str], ...]  # (source label, rendered value)
    resolution: str


@dataclass(frozen=True, slots=True)
class ResolvedField:
    value: Any
    provenance: FieldProvenance


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    route: RouteIdentity
    context_window_tokens: ResolvedField
    max_output_tokens: ResolvedField
    supports_tools: ResolvedField
    supports_reasoning: ResolvedField
    supports_streaming: ResolvedField
    supports_prompt_cache: ResolvedField
    supports_structured_output: ResolvedField
    modalities: ResolvedField
    input_price_per_token_usd: ResolvedField
    output_price_per_token_usd: ResolvedField
    cache_read_input_price_per_token_usd: ResolvedField
    conflicts: tuple[CapabilityConflict, ...] = ()

    def field(self, name: str) -> ResolvedField:
        if name not in _CAPABILITY_FIELDS:
            raise KeyError(f"unknown capability field: {name}")
        return getattr(self, name)


@dataclass(frozen=True, slots=True)
class CapabilityClaim:
    """A partial capability statement from one source tier.

    Absence of a field (missing key) is never evidence of nonsupport.
    """

    tier: SourceTier
    source: str
    values: Mapping[str, object]
    retrieved_at: str | None = None
    raw_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorOverride:
    """Tier-1 override scoped to an exact transport + endpoint + model."""

    route: RouteIdentity
    values: Mapping[str, object]
    source: str = "operator"


class LiveMetadataHook(Protocol):
    """Tier-2 hook: authoritative capabilities exposed by a live transport."""

    def capabilities(self, route: RouteIdentity) -> CapabilityClaim | None: ...


class CatalogueHook(Protocol):
    """Tier-4/5 hook: provider-native or secondary public catalogue."""

    def lookup(self, route: RouteIdentity) -> CapabilityClaim | None: ...


class RuntimeMetadataHook(Protocol):
    """Tier-6 hook: runtime metadata from Ollama, vLLM, llama.cpp, Hugging Face."""

    def metadata(self, route: RouteIdentity) -> CapabilityClaim | None: ...


class _StubCatalogueHook:
    """Unimplemented placeholder for remote catalogue fetching.

    ``lookup`` always returns ``None``: the tier contributes no claims, so
    resolution falls through to the explicitly labelled tier-7 assumption
    rather than presenting catalogue-backed values that were never fetched.

    Only the OpenRouter tier-5 hook is implemented (see
    :class:`_OpenRouterCatalogueHook`); provider-native and LiteLLM catalogues
    remain honest stubs pending bounded-schedule refresh against their
    documented JSON APIs.
    """

    def __init__(self, tier: SourceTier, source: str) -> None:
        self.tier, self.source = tier, source

    def lookup(self, route: RouteIdentity) -> CapabilityClaim | None:
        return None


_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai"
_OPENROUTER_DEFAULT_TTL_SECONDS = 600.0
_OPENROUTER_DEFAULT_TIMEOUT_SECONDS = 2.5


class _OpenRouterCatalogueHook:
    """Tier-5 resolver against OpenRouter's public ``GET /api/v1/models``.

    Safety gating: the hook only issues a network call when the route's
    endpoint host is ``openrouter.ai`` (or a subdomain), or when
    ``match_vendor_slug`` is explicitly enabled and the model id carries an
    OpenRouter-style ``vendor/model`` slug. Unrelated providers therefore
    never trigger surprise traffic; opting into the slug heuristic is the
    caller's decision.

    The whole model list is fetched once and cached in-process for
    ``ttl_seconds``; after a successful fetch the raw catalogue is also
    persisted to ``cache_path`` (defaulting to the capability cache directory)
    as a last-good offline fallback. On any network error or timeout the hook
    serves the stale in-memory copy, then the persisted copy, and otherwise
    returns ``None`` so resolution falls through to lower tiers — it never
    raises into resolution. Fallback claims keep
    ``SourceTier.SECONDARY_CATALOGUE`` and their original ``retrieved_at``,
    so reloaded evidence is never presented as fresher or more confident
    than it is; the claims also ride the registry's own snapshot save/load
    like any other tier-5 claim.
    """

    def __init__(
        self,
        *,
        base_url: str = _OPENROUTER_DEFAULT_BASE_URL,
        ttl_seconds: float = _OPENROUTER_DEFAULT_TTL_SECONDS,
        timeout_seconds: float = _OPENROUTER_DEFAULT_TIMEOUT_SECONDS,
        match_vendor_slug: bool = False,
        cache_path: Path | None = None,
        client: httpx.Client | None = None,
        api_key: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tier, self.source = SourceTier.SECONDARY_CATALOGUE, "openrouter"
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._match_vendor_slug = match_vendor_slug
        self._cache_path = cache_path or (
            ModelCapabilityRegistry.snapshot_path().parent / "openrouter-models.json"
        )
        self._client = client
        self._api_key = api_key
        self._clock = clock
        self._models: dict[str, Mapping[str, Any]] | None = None
        self._fetched_at: float | None = None
        self._retrieved_at: str | None = None

    def _matches(self, route: RouteIdentity) -> bool:
        host = urlparse(route.endpoint).hostname or ""
        if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
            return True
        return self._match_vendor_slug and "/" in route.model

    def lookup(self, route: RouteIdentity) -> CapabilityClaim | None:
        if not self._matches(route):
            return None
        models = self._catalogue()
        if not models:
            return None
        entry = models.get(route.model) or models.get(route.model.lower())
        if entry is None:
            return None
        values: dict[str, object] = {}
        context = entry.get("context_length")
        if isinstance(context, (int, float)) and not isinstance(context, bool) and context > 0:
            values["context_window_tokens"] = int(context)
        top_provider = entry.get("top_provider")
        max_output = None
        if isinstance(top_provider, Mapping):
            max_output = top_provider.get("max_completion_tokens")
        if max_output is None:
            max_output = entry.get("max_completion_tokens")
        if (
            isinstance(max_output, (int, float))
            and not isinstance(max_output, bool)
            and max_output > 0
        ):
            values["max_output_tokens"] = int(max_output)
        architecture = entry.get("architecture")
        if isinstance(architecture, Mapping):
            inputs = architecture.get("input_modalities")
            if isinstance(inputs, list) and inputs:
                values["modalities"] = tuple(str(item) for item in inputs)
        # OpenRouter pricing.prompt/completion are USD-per-token strings.
        pricing = entry.get("pricing")
        if isinstance(pricing, Mapping):
            for key, field_name in (
                ("prompt", "input_price_per_token_usd"),
                ("completion", "output_price_per_token_usd"),
            ):
                try:
                    rate = float(pricing.get(key))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                if rate >= 0:
                    values[field_name] = rate
        if not values:
            return None
        return CapabilityClaim(
            tier=self.tier,
            source=self.source,
            values=values,
            retrieved_at=self._retrieved_at,
            raw_model_id=str(entry.get("id") or route.model),
        )

    def _catalogue(self) -> dict[str, Mapping[str, Any]]:
        now = self._clock()
        if (
            self._models is not None
            and self._fetched_at is not None
            and now - self._fetched_at < self._ttl
        ):
            return self._models
        try:
            self._fetch()
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            pass  # fall through to stale in-memory, then the last-good file
        if self._models is not None:
            return self._models
        return self._load_last_good()

    def _fetch(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=httpx.Timeout(self._timeout))
        try:
            response = client.get(f"{self._base_url}/api/v1/models", headers=headers)
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                client.close()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, list):
            raise TypeError("unexpected OpenRouter models payload")
        models: dict[str, Mapping[str, Any]] = {}
        for entry in data:
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                models[entry["id"]] = entry
        self._models = models
        self._fetched_at = self._clock()
        self._retrieved_at = datetime.now(UTC).isoformat()
        self._persist_last_good(payload)

    def _persist_last_good(self, payload: object) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"saved_at": self._retrieved_at, "payload": payload}),
                encoding="utf-8",
            )
        except OSError:
            pass  # a cache write failure must never break resolution

    def _load_last_good(self) -> dict[str, Mapping[str, Any]]:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            payload = raw.get("payload", {})
            data = payload.get("data", [])
            models = {
                entry["id"]: entry
                for entry in data
                if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}
        if models:
            self._models = models
            self._fetched_at = None  # never TTL-fresh; refetch on next lookup
            self._retrieved_at = str(raw.get("saved_at") or "") or None
        return models


def openrouter_catalogue(
    *,
    base_url: str = _OPENROUTER_DEFAULT_BASE_URL,
    ttl_seconds: float = _OPENROUTER_DEFAULT_TTL_SECONDS,
    timeout_seconds: float = _OPENROUTER_DEFAULT_TIMEOUT_SECONDS,
    match_vendor_slug: bool = False,
    cache_path: Path | None = None,
    client: httpx.Client | None = None,
    api_key: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CatalogueHook:
    """OpenRouter tier-5 catalogue resolver; see :class:`_OpenRouterCatalogueHook`."""
    return _OpenRouterCatalogueHook(
        base_url=base_url,
        ttl_seconds=ttl_seconds,
        timeout_seconds=timeout_seconds,
        match_vendor_slug=match_vendor_slug,
        cache_path=cache_path,
        client=client,
        api_key=api_key,
        clock=clock,
    )


_MODELS_DEV_DEFAULT_BASE_URL = "https://models.dev"
_MODELS_DEV_DEFAULT_TTL_SECONDS = 600.0
_MODELS_DEV_DEFAULT_TIMEOUT_SECONDS = 2.5
_MODELS_DEV_DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# Endpoint host -> models.dev provider id. Deliberately small and explicit:
# an unknown host means no claim, never a guessed provider id.
_MODELS_DEV_HOST_PROVIDERS: Mapping[str, str] = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.deepseek.com": "deepseek",
    "generativelanguage.googleapis.com": "google",
    "api.moonshot.ai": "moonshotai",
    "api.moonshot.cn": "moonshotai",
}


class _ModelsDevCatalogueHook:
    """Tier-5 resolver against models.dev's public ``GET /api.json``.

    Safety gating mirrors the OpenRouter hook: the hook only issues a network
    call when the route's endpoint host maps to a known models.dev provider id
    (see ``_MODELS_DEV_HOST_PROVIDERS``); unrelated endpoints never trigger
    surprise traffic. The whole catalogue is fetched once and cached in-process
    for ``ttl_seconds``; after a successful fetch the raw payload is persisted
    to ``cache_path`` as a last-good offline fallback. On any network error,
    timeout, or malformed/oversized payload the hook serves the stale in-memory
    copy, then the persisted copy, and otherwise returns ``None`` so resolution
    falls through to lower tiers — it never raises into resolution.

    Per-model mapping (api.json schema): ``limit.context`` /
    ``limit.output`` -> context/output windows, ``reasoning`` and
    ``tool_call`` booleans -> support flags, ``modalities.input`` ->
    modalities. ``cost.input`` / ``cost.output`` (USD per million tokens)
    map to the per-token pricing fields; other ``cost.*`` rates
    (cache tiers) are not yet modelled. Fields the source does not provide
    are never invented. Model ids match exactly, then by longest prefix
    like the built-in catalogue so dated variants resolve to their family
    entry.
    """

    def __init__(
        self,
        *,
        base_url: str = _MODELS_DEV_DEFAULT_BASE_URL,
        ttl_seconds: float = _MODELS_DEV_DEFAULT_TTL_SECONDS,
        timeout_seconds: float = _MODELS_DEV_DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = _MODELS_DEV_DEFAULT_MAX_BYTES,
        cache_path: Path | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tier, self.source = SourceTier.SECONDARY_CATALOGUE, "models.dev"
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._cache_path = cache_path or (
            ModelCapabilityRegistry.snapshot_path().parent / "models-dev-api.json"
        )
        self._client = client
        self._clock = clock
        self._providers: dict[str, Mapping[str, Any]] | None = None
        self._fetched_at: float | None = None
        self._retrieved_at: str | None = None

    def lookup(self, route: RouteIdentity) -> CapabilityClaim | None:
        host = urlparse(route.endpoint).hostname or ""
        provider = _MODELS_DEV_HOST_PROVIDERS.get(host)
        if provider is None:
            return None
        providers = self._catalogue()
        models = providers.get(provider)
        if not isinstance(models, Mapping):
            return None
        entry = models.get(route.model) or models.get(route.model.lower())
        if entry is None:
            model = route.model.lower()
            match = max(
                (model_id for model_id in models if model.startswith(model_id.lower())),
                key=len,
                default=None,
            )
            entry = models.get(match) if match is not None else None
        if not isinstance(entry, Mapping):
            return None
        values: dict[str, object] = {}
        limit = entry.get("limit")
        if isinstance(limit, Mapping):
            context = limit.get("context")
            if isinstance(context, (int, float)) and not isinstance(context, bool) and context > 0:
                values["context_window_tokens"] = int(context)
            max_output = limit.get("output")
            if (
                isinstance(max_output, (int, float))
                and not isinstance(max_output, bool)
                and max_output > 0
            ):
                values["max_output_tokens"] = int(max_output)
        reasoning = entry.get("reasoning")
        if isinstance(reasoning, bool):
            values["supports_reasoning"] = reasoning
        tool_call = entry.get("tool_call")
        if isinstance(tool_call, bool):
            values["supports_tools"] = tool_call
        modalities = entry.get("modalities")
        if isinstance(modalities, Mapping):
            inputs = modalities.get("input")
            if isinstance(inputs, list) and inputs:
                values["modalities"] = tuple(str(item) for item in inputs)
        # models.dev cost.input/output are USD per million tokens.
        cost = entry.get("cost")
        if isinstance(cost, Mapping):
            for key, field_name in (
                ("input", "input_price_per_token_usd"),
                ("output", "output_price_per_token_usd"),
                ("cache_read", "cache_read_input_price_per_token_usd"),
            ):
                rate = cost.get(key)
                if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= 0:
                    values[field_name] = float(rate) / 1_000_000
        if not values:
            return None
        return CapabilityClaim(
            tier=self.tier,
            source=self.source,
            values=values,
            retrieved_at=self._retrieved_at,
            raw_model_id=str(entry.get("id") or route.model),
        )

    def _catalogue(self) -> dict[str, Mapping[str, Any]]:
        now = self._clock()
        if (
            self._providers is not None
            and self._fetched_at is not None
            and now - self._fetched_at < self._ttl
        ):
            return self._providers
        try:
            self._fetch()
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            pass  # fall through to stale in-memory, then the last-good file
        if self._providers is not None:
            return self._providers
        return self._load_last_good()

    def _fetch(self) -> None:
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(timeout=httpx.Timeout(self._timeout))
        try:
            response = client.get(f"{self._base_url}/api.json")
            response.raise_for_status()
            if len(response.content) > self._max_bytes:
                raise ValueError("models.dev payload exceeds size ceiling")
            payload = response.json()
        finally:
            if owns_client:
                client.close()
        providers = self._parse_providers(payload)
        if not providers:
            raise TypeError("unexpected models.dev api.json payload")
        self._providers = providers
        self._fetched_at = self._clock()
        self._retrieved_at = datetime.now(UTC).isoformat()
        self._persist_last_good(payload)

    @staticmethod
    def _parse_providers(payload: object) -> dict[str, Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            return {}
        providers: dict[str, Mapping[str, Any]] = {}
        for provider_id, provider_entry in payload.items():
            if not isinstance(provider_id, str) or not isinstance(provider_entry, Mapping):
                continue
            models = provider_entry.get("models")
            if not isinstance(models, Mapping):
                continue
            parsed = {
                model_id: entry
                for model_id, entry in models.items()
                if isinstance(model_id, str) and isinstance(entry, Mapping)
            }
            if parsed:
                providers[provider_id] = parsed
        return providers

    def _persist_last_good(self, payload: object) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"saved_at": self._retrieved_at, "payload": payload}),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            pass  # a cache write failure must never break resolution

    def _load_last_good(self) -> dict[str, Mapping[str, Any]]:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            providers = self._parse_providers(raw.get("payload"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}
        if providers:
            self._providers = providers
            self._fetched_at = None  # never TTL-fresh; refetch on next lookup
            self._retrieved_at = str(raw.get("saved_at") or "") or None
        return providers


def models_dev_catalogue(
    *,
    base_url: str = _MODELS_DEV_DEFAULT_BASE_URL,
    ttl_seconds: float = _MODELS_DEV_DEFAULT_TTL_SECONDS,
    timeout_seconds: float = _MODELS_DEV_DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _MODELS_DEV_DEFAULT_MAX_BYTES,
    cache_path: Path | None = None,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CatalogueHook:
    """models.dev tier-5 catalogue resolver; see :class:`_ModelsDevCatalogueHook`."""
    return _ModelsDevCatalogueHook(
        base_url=base_url,
        ttl_seconds=ttl_seconds,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        cache_path=cache_path,
        client=client,
        clock=clock,
    )


def litellm_catalogue() -> CatalogueHook:
    return _StubCatalogueHook(SourceTier.SECONDARY_CATALOGUE, "litellm")


def provider_native_catalogue(source: str) -> CatalogueHook:
    return _StubCatalogueHook(SourceTier.PROVIDER_CATALOGUE, source)


# Reviewed built-in catalogue (tier 3). Keys are model-ID prefixes; the longest
# matching prefix wins so dated variants (``gpt-5.4-heldout-v1``) resolve to
# their family entry. Values are reviewed at authoring time and must be updated
# deliberately, never scraped at runtime.
BUILTIN_CATALOG: Mapping[str, Mapping[str, object]] = {
    "gpt-5": {
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image"),
    },
    "claude-opus": {
        "context_window_tokens": 200_000,
        "max_output_tokens": 64_000,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image"),
    },
    "claude-sonnet": {
        "context_window_tokens": 200_000,
        "max_output_tokens": 64_000,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image"),
    },
    "claude-haiku": {
        "context_window_tokens": 200_000,
        "max_output_tokens": 8_192,
        "supports_tools": True,
        "supports_reasoning": False,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image"),
    },
    # DeepSeek V4 limits reconciled with models.dev on 2026-10-13. The official
    # endpoint's /models response exposes ids only, so models.dev remains the
    # live corroborating source for output limits.
    "deepseek-v4-pro": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text",),
    },
    "deepseek-v4-flash-vision-exp": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image"),
    },
    "deepseek-v4-flash": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text",),
    },
    "deepseek-v4": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 393_216,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text",),
    },
    "kimi-k2": {
        "context_window_tokens": 262_144,
        "max_output_tokens": 8_192,
        "supports_tools": True,
        "supports_reasoning": False,
        "supports_streaming": True,
        "supports_prompt_cache": False,
        "supports_structured_output": True,
        "modalities": ("text",),
    },
    "gemini-2.5-pro": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image", "audio"),
    },
    "gemini-2.5-flash": {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_streaming": True,
        "supports_prompt_cache": True,
        "supports_structured_output": True,
        "modalities": ("text", "image", "audio"),
    },
}

_CAPABILITY_ALIASES = {
    "tools": "supports_tools",
    "reasoning": "supports_reasoning",
    "streaming": "supports_streaming",
    "prompt_cache": "supports_prompt_cache",
    "structured_output": "supports_structured_output",
}


def overrides_from_provider_config(
    provider: Mapping[str, object],
    *,
    transport: str = "openai_compatible",
    endpoint: str | None = None,
) -> tuple[OperatorOverride, ...]:
    """Consume the existing ``[provider]`` config surface as tier-1 overrides.

    Reads ``model``, ``base_url``, ``context_window_tokens``, ``vision``,
    ``model_capabilities``, and ``model_pricing`` from the same mapping
    merlin-config validates, so existing exact-route operator overrides keep
    working unchanged. ``model_pricing`` rates (USD per million tokens) are
    carried as per-token pricing fields.
    """
    model = str(provider.get("model", ""))
    if not model:
        return ()
    route = RouteIdentity(
        transport=transport,
        endpoint=endpoint if endpoint is not None else str(provider.get("base_url", "")),
        model=model,
    )
    values: dict[str, object] = {}
    context = provider.get("context_window_tokens")
    if isinstance(context, (int, float)) and not isinstance(context, bool):
        values["context_window_tokens"] = int(context)
    capabilities = provider.get("model_capabilities", {})
    if isinstance(capabilities, Mapping):
        configured = capabilities.get(model)
        if isinstance(configured, list):
            for item in configured:
                field_name = _CAPABILITY_ALIASES.get(str(item))
                if field_name is not None:
                    values[field_name] = True
                elif str(item) == "vision":
                    values["modalities"] = ("text", "image")
    if provider.get("vision"):
        values["modalities"] = ("text", "image")
    pricing = provider.get("model_pricing", {})
    if isinstance(pricing, Mapping):
        rates = pricing.get(model)
        if isinstance(rates, Mapping):
            for key, field_name in (
                ("input_cost_per_million", "input_price_per_token_usd"),
                ("output_cost_per_million", "output_price_per_token_usd"),
                ("cache_read_input_cost_per_million", "cache_read_input_price_per_token_usd"),
            ):
                rate = rates.get(key)
                if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= 0:
                    values[field_name] = float(rate) / 1_000_000
    if not values:
        return ()
    return (OperatorOverride(route=route, values=values),)


def _coerce(field_name: str, value: object) -> object:
    if field_name == "modalities":
        if isinstance(value, str):
            return (value,)
        assert isinstance(value, Iterable)
        return tuple(str(item) for item in value)
    if isinstance(value, bool) or value is None:
        return value
    if field_name in ("context_window_tokens", "max_output_tokens"):
        assert isinstance(value, (int, float, str))
        return int(value)
    if field_name in (
        "input_price_per_token_usd",
        "output_price_per_token_usd",
        "cache_read_input_price_per_token_usd",
    ):
        assert isinstance(value, (int, float, str))
        return float(value)
    return bool(value)


def _smallest_credible(field_name: str, values: list[object]) -> object:
    if field_name == "modalities":
        sets = [set(v) for v in values if isinstance(v, Iterable) and not isinstance(v, str)]
        common = set.intersection(*sets) if sets else set()
        return tuple(sorted(common)) or ("text",)
    if all(isinstance(v, bool) for v in values):
        return all(values)
    numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return min(numeric) if numeric else values[0]


class ModelCapabilityRegistry:
    """Resolves normalized per-route capabilities with per-field provenance."""

    def __init__(
        self,
        *,
        overrides: Iterable[OperatorOverride] = (),
        builtin_catalog: Mapping[str, Mapping[str, object]] = BUILTIN_CATALOG,
        live_hooks: Iterable[LiveMetadataHook] = (),
        catalogue_hooks: Iterable[CatalogueHook] = (),
        runtime_hooks: Iterable[RuntimeMetadataHook] = (),
        snapshot_claims: Mapping[RouteIdentity, Iterable[CapabilityClaim]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._overrides = list(overrides)
        self._builtin = dict(builtin_catalog)
        self._live_hooks = list(live_hooks)
        self._catalogue_hooks = list(catalogue_hooks)
        self._runtime_hooks = list(runtime_hooks)
        self._snapshot_claims = (
            {}
            if snapshot_claims is None
            else {route: list(claims) for route, claims in snapshot_claims.items()}
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._overflow_limits: dict[RouteIdentity, int] = {}
        self._observed_lower_bounds: dict[RouteIdentity, int] = {}

    def add_override(self, override: OperatorOverride) -> None:
        self._overrides = [o for o in self._overrides if o.route != override.route]
        self._overrides.append(override)

    def resolve_model(
        self, model: str, *, transport: str = "openai_compatible", endpoint: str = ""
    ) -> ModelCapabilities:
        """Resolve by model ID alone — the duck-typed seam for kernel/runtime consumers.

        Callers that only know the routed model name (Kernel, WorkerRuntime) use
        this instead of constructing a :class:`RouteIdentity`; per-field
        provenance still labels which tier answered.
        """
        return self.resolve(RouteIdentity(transport=transport, endpoint=endpoint, model=model))

    def resolve(self, route: RouteIdentity) -> ModelCapabilities:
        claims = self._gather_claims(route)
        resolved: dict[str, ResolvedField] = {}
        conflicts: list[CapabilityConflict] = []
        for field_name in _CAPABILITY_FIELDS:
            resolved[field_name], conflict = self._resolve_field(route, field_name, claims)
            if conflict is not None:
                conflicts.append(conflict)
        limit = self._overflow_limits.get(route)
        if limit is not None:
            current = resolved["context_window_tokens"]
            if current.value is None or limit < current.value:
                provenance = replace(
                    current.provenance,
                    conflicts=current.provenance.conflicts
                    + (
                        (
                            f"provider context-overflow lowered effective limit to {limit} "
                            f"(advertised {current.value})"
                        ),
                    ),
                )
                resolved["context_window_tokens"] = ResolvedField(limit, provenance)
        warning = self._operator_context_warning(route, resolved["context_window_tokens"])
        if warning is not None:
            current = resolved["context_window_tokens"]
            resolved["context_window_tokens"] = ResolvedField(
                current.value,
                replace(
                    current.provenance,
                    conflicts=current.provenance.conflicts + (warning,),
                ),
            )
        return ModelCapabilities(route=route, conflicts=tuple(conflicts), **resolved)

    def _operator_context_warning(self, route: RouteIdentity, field: ResolvedField) -> str | None:
        """Flag absurd tier-1 context windows; never clamps or rejects them."""
        if field.provenance.tier is not SourceTier.OPERATOR_OVERRIDE:
            return None
        value = field.value
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        if value > _OPERATOR_CONTEXT_HARD_CEILING:
            return (
                f"operator context_window_tokens {value} exceeds the sanity ceiling "
                f"{_OPERATOR_CONTEXT_HARD_CEILING}; verify the model really supports it"
            )
        builtin = self._builtin_claim(route, self._now().isoformat())
        if builtin is not None:
            catalogued = builtin.values.get("context_window_tokens")
            if isinstance(catalogued, int) and value > catalogued:
                return (
                    f"operator context_window_tokens {value} exceeds the known catalog "
                    f"value {catalogued} for {builtin.source}; verify the override"
                )
        return None

    def record_context_overflow(self, route: RouteIdentity, observed_limit: int) -> None:
        """A provider overflow response may lower the assumed effective limit."""
        if observed_limit < 1:
            raise ValueError("observed overflow limit must be positive")
        current = self._overflow_limits.get(route)
        if current is None or observed_limit < current:
            self._overflow_limits[route] = observed_limit

    def observe_successful_request(self, route: RouteIdentity, prompt_tokens: int) -> None:
        """A successful smaller request establishes only a lower bound.

        It must never increase the advertised maximum; the bound is recorded
        for diagnostics only.
        """
        current = self._observed_lower_bounds.get(route)
        if current is None or prompt_tokens > current:
            self._observed_lower_bounds[route] = prompt_tokens

    def save_snapshot(self, path: Path) -> None:
        """Cache the current normalized view of all known routes for offline startup."""
        routes = {override.route for override in self._overrides}
        routes |= set(self._overflow_limits)
        snapshot = {
            "saved_at": self._now().isoformat(),
            "records": [
                self._record_dict(self.resolve(route)) for route in sorted(routes, key=str)
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    @staticmethod
    def snapshot_path(state_directory: Path | None = None) -> Path:
        directory = state_directory or (Path.home() / ".merlin")
        return directory / "cache" / "capabilities-snapshot.json"

    def _gather_claims(self, route: RouteIdentity) -> list[CapabilityClaim]:
        retrieved_at = self._now().isoformat()
        claims = [
            CapabilityClaim(
                tier=SourceTier.OPERATOR_OVERRIDE,
                source=override.source,
                values=override.values,
                retrieved_at=retrieved_at,
                raw_model_id=route.model,
            )
            for override in self._overrides
            if override.route == route
        ]
        for live_hook in self._live_hooks:
            claim = live_hook.capabilities(route)
            if claim is not None:
                claims.append(claim)
        builtin = self._builtin_claim(route, retrieved_at)
        if builtin is not None:
            claims.append(builtin)
        for catalogue_hook in self._catalogue_hooks:
            claim = catalogue_hook.lookup(route)
            if claim is not None:
                claims.append(claim)
        for runtime_hook in self._runtime_hooks:
            claim = runtime_hook.metadata(route)
            if claim is not None:
                claims.append(claim)
        claims.extend(self._snapshot_claims.get(route, ()))
        return claims

    def _builtin_claim(self, route: RouteIdentity, retrieved_at: str) -> CapabilityClaim | None:
        model = route.model.lower()
        match = max(
            (prefix for prefix in self._builtin if model.startswith(prefix)),
            key=len,
            default=None,
        )
        if match is None:
            return None
        return CapabilityClaim(
            tier=SourceTier.BUILTIN_REGISTRY,
            source=f"builtin:{match}",
            values=self._builtin[match],
            retrieved_at=retrieved_at,
            raw_model_id=route.model,
        )

    def _resolve_field(
        self, route: RouteIdentity, field_name: str, claims: list[CapabilityClaim]
    ) -> tuple[ResolvedField, CapabilityConflict | None]:
        per_tier: dict[SourceTier, list[tuple[CapabilityClaim, object]]] = {}
        for claim in claims:
            if field_name in claim.values and claim.values[field_name] is not None:
                value = _coerce(field_name, claim.values[field_name])
                per_tier.setdefault(claim.tier, []).append((claim, value))
        if not per_tier:
            provenance = FieldProvenance(
                tier=SourceTier.CONSERVATIVE_ASSUMPTION,
                retrieved_at=self._now().isoformat(),
                raw_model_id=route.model,
                transport=route.transport,
                endpoint=route.endpoint,
                confidence=_TIER_CONFIDENCE[SourceTier.CONSERVATIVE_ASSUMPTION],
            )
            return ResolvedField(_ASSUMPTIONS[field_name], provenance), None

        winning_tier = min(per_tier)
        winner_claim, winner_value = per_tier[winning_tier][0]
        other_claims = [
            (claim, value)
            for tier, entries in per_tier.items()
            for claim, value in entries
            if tier != winning_tier
        ]
        differing = [(claim, value) for claim, value in other_claims if value != winner_value]
        conflict: CapabilityConflict | None = None
        resolution = ""
        chosen = winner_value
        route_specific = winning_tier in (
            SourceTier.OPERATOR_OVERRIDE,
            SourceTier.LIVE_ROUTE_METADATA,
        )
        provider_native = any(claim.tier == SourceTier.PROVIDER_CATALOGUE for claim, _ in differing)
        if differing and not route_specific and not provider_native:
            candidates = [winner_value] + [value for _, value in differing]
            chosen = _smallest_credible(field_name, candidates)
            resolution = f"smallest credible of {len(candidates)} disagreeing sources"
        elif differing:
            resolution = (
                f"tier {int(winning_tier)} "
                f"{'route-specific' if route_specific else 'provider-native'} value preferred"
            )
        if differing:
            rendered = [(winner_claim.source, repr(winner_value))]
            rendered += [(claim.source, repr(value)) for claim, value in differing]
            conflict = CapabilityConflict(
                field=field_name, values=tuple(rendered), resolution=resolution
            )
        provenance = FieldProvenance(
            tier=winning_tier,
            retrieved_at=winner_claim.retrieved_at,
            raw_model_id=winner_claim.raw_model_id or route.model,
            transport=route.transport,
            endpoint=route.endpoint,
            confidence=_TIER_CONFIDENCE[winning_tier],
            corroborating_sources=tuple(
                claim.source for claim, value in other_claims if value == winner_value
            ),
            conflicts=() if conflict is None else (f"{conflict.field}: {conflict.resolution}",),
        )
        return ResolvedField(chosen, provenance), conflict

    def _record_dict(self, capabilities: ModelCapabilities) -> dict[str, object]:
        record = asdict(capabilities)
        for field_name in _CAPABILITY_FIELDS:
            record[field_name]["provenance"]["tier"] = int(record[field_name]["provenance"]["tier"])
        return record


def load_snapshot_claims(path: Path) -> dict[RouteIdentity, list[CapabilityClaim]]:
    """Parse a saved snapshot into per-route claims at their recorded tiers.

    Tier-7 assumptions are never reloaded as evidence — they are recomputed on
    demand so an offline startup keeps labelling unknowns as assumptions.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    claims: dict[RouteIdentity, list[CapabilityClaim]] = {}
    records = raw.get("records", []) if isinstance(raw, Mapping) else []
    for record in records:
        try:
            route_data = record["route"]
            route = RouteIdentity(
                transport=str(route_data["transport"]),
                endpoint=str(route_data["endpoint"]),
                model=str(route_data["model"]),
            )
            per_tier: dict[SourceTier, dict[str, object]] = {}
            retrieved_at: str | None = None
            for field_name in _CAPABILITY_FIELDS:
                entry = record.get(field_name)
                if not isinstance(entry, Mapping):
                    continue
                provenance = entry.get("provenance", {})
                tier = SourceTier(int(provenance["tier"]))
                if tier is SourceTier.CONSERVATIVE_ASSUMPTION:
                    continue
                per_tier.setdefault(tier, {})[field_name] = entry.get("value")
                retrieved_at = retrieved_at or provenance.get("retrieved_at")
            for tier, values in per_tier.items():
                claims.setdefault(route, []).append(
                    CapabilityClaim(
                        tier=tier,
                        source="snapshot",
                        values=values,
                        retrieved_at=retrieved_at,
                        raw_model_id=route.model,
                    )
                )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # a malformed cached record must never block startup
    return claims


def load_last_good_snapshot(path: Path) -> dict[RouteIdentity, list[CapabilityClaim]]:
    """Load the cached snapshot for offline startup; an absent or corrupt cache yields nothing."""
    try:
        return load_snapshot_claims(path)
    except (OSError, json.JSONDecodeError):
        return {}
