"""Exact model-to-transport routing for heterogeneous provider deployments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from merlin_contracts import ChatRequest, DynamicToolHandler, ModelEvent, Provider

from .capabilities import RouteIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SharedProviderCapabilities:
    """Transport facts for a shared endpoint; model-specific facts stay unknown.

    ``context_window_tokens`` and ``vision`` stay ``None`` (explicit unknown)
    unless the capability registry resolves them from operator config or a
    catalogue; an unknown model never silently inherits the endpoint's window.
    """

    streaming: bool
    tools: bool
    usage_in_stream: bool
    model: str
    vision: bool | None = None
    context_window_tokens: int | None = None


def _validate_explicit_route_model(provider: Provider, model: str) -> None:
    identity = getattr(provider, "route_identity", None)
    capabilities = getattr(provider, "capabilities", None)
    declarations = (
        ("route identity", getattr(identity, "model", None)),
        ("capabilities", getattr(capabilities, "model", None)),
    )
    for source, declared_model in declarations:
        if isinstance(declared_model, str) and declared_model and declared_model != model:
            raise ValueError(
                "explicit provider route model mismatch: "
                f"requested {model!r}, {source} declares {declared_model!r}"
            )


def _fallback_capabilities(provider: Provider, model: str) -> object | None:
    capabilities = getattr(provider, "capabilities", None)
    if capabilities is None:
        return None
    identity = getattr(provider, "route_identity", None)
    declared_model = getattr(capabilities, "model", None) or getattr(identity, "model", None)
    if not declared_model or declared_model == model:
        return capabilities
    return _SharedProviderCapabilities(
        streaming=bool(getattr(capabilities, "streaming", False)),
        tools=bool(getattr(capabilities, "tools", False)),
        usage_in_stream=bool(getattr(capabilities, "usage_in_stream", False)),
        model=model,
    )


class ModelRoutingProvider:
    def __init__(
        self,
        default: Provider,
        routes: Mapping[str, Provider],
        capability_registry: object | None = None,
    ) -> None:
        if any(not model.strip() for model in routes):
            raise ValueError("provider route model names must not be empty")
        self.default = default
        self.routes = dict(routes)
        self.capability_registry = capability_registry

    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        provider = self.routes.get(request.model, self.default)
        if request.model in self.routes:
            _validate_explicit_route_model(provider, request.model)
        logger.info(
            "provider request routed model=%s provider=%s explicit_route=%s interactive=%s",
            request.model,
            type(provider).__name__,
            request.model in self.routes,
            False,
        )
        return provider.stream(request)

    def capabilities_for_model(self, model: str) -> object | None:
        """Return capabilities for the exact routed provider, never a fleet-wide minimum."""
        provider = self.routes.get(model)
        if provider is not None:
            _validate_explicit_route_model(provider, model)
            capabilities = getattr(provider, "capabilities", None)
        else:
            capabilities = _fallback_capabilities(self.default, model)
        return self._registry_backed_capabilities(model, capabilities)

    def _registry_backed_capabilities(
        self, model: str, capabilities: object | None
    ) -> object | None:
        """Fill model-specific unknowns from the capability registry.

        Tier-1 operator overrides win over the built-in catalogue, which wins
        over live catalogue hooks. A model the registry can only resolve at its
        tier-7 assumption stays an explicit unknown (``context_window_tokens``
        is ``None``) and is logged — never silently sized.
        """
        window = getattr(capabilities, "context_window_tokens", None)
        if isinstance(window, int) and not isinstance(window, bool):
            return capabilities
        resolve = getattr(self.capability_registry, "resolve_model", None)
        if not callable(resolve):
            return capabilities
        identity = self.route_identity_for_model(model)
        resolved = resolve(
            model,
            transport=str(getattr(identity, "transport", "openai_compatible")),
            endpoint=str(getattr(identity, "endpoint", "")),
        )
        field = resolved.context_window_tokens
        value = getattr(field, "value", None)
        assumed = bool(getattr(getattr(field, "provenance", None), "assumed", True))
        if assumed or not isinstance(value, int) or isinstance(value, bool) or value < 1:
            logger.warning(
                "capabilities unknown for model=%s transport=%s endpoint=%s; "
                "returning explicit unknown entry (no context window assumed)",
                model,
                getattr(identity, "transport", ""),
                getattr(identity, "endpoint", ""),
            )
            return capabilities
        modalities = getattr(resolved.modalities, "value", None) or ()
        vision = True if "image" in modalities else getattr(capabilities, "vision", None)
        return _SharedProviderCapabilities(
            streaming=bool(getattr(capabilities, "streaming", False)),
            tools=bool(getattr(capabilities, "tools", False)),
            usage_in_stream=bool(getattr(capabilities, "usage_in_stream", False)),
            model=model,
            vision=vision,
            context_window_tokens=value,
        )

    def route_identity_for_model(self, model: str) -> object | None:
        """Return the exact routed provider's RouteIdentity (transport + endpoint + model)."""
        provider = self.routes.get(model)
        if provider is not None:
            _validate_explicit_route_model(provider, model)
            return getattr(provider, "route_identity", None)
        identity = getattr(self.default, "route_identity", None)
        if identity is None or getattr(identity, "model", None) == model:
            return identity
        return RouteIdentity(
            transport=str(getattr(identity, "transport", "")),
            endpoint=str(getattr(identity, "endpoint", "")),
            model=model,
        )

    @property
    def tool_call_signatures(self) -> Mapping[str, str]:
        """Merged thoughtSignature map from routed providers that capture them."""
        merged: dict[str, str] = {}
        providers = {id(provider): provider for provider in (self.default, *self.routes.values())}
        for provider in providers.values():
            signatures = getattr(provider, "tool_call_signatures", None)
            if signatures:
                merged.update(signatures)
        return merged

    def stream_with_tools(
        self, request: ChatRequest, handler: DynamicToolHandler
    ) -> AsyncIterator[ModelEvent]:
        provider = self.routes.get(request.model, self.default)
        if request.model in self.routes:
            _validate_explicit_route_model(provider, request.model)
        interactive = getattr(provider, "stream_with_tools", None)
        logger.info(
            "provider request routed model=%s provider=%s explicit_route=%s interactive=%s",
            request.model,
            type(provider).__name__,
            request.model in self.routes,
            callable(interactive),
        )
        if not callable(interactive):
            return provider.stream(request)
        return interactive(request, handler)

    async def close(self) -> None:
        providers = {id(provider): provider for provider in (self.default, *self.routes.values())}
        await asyncio.gather(
            *(
                close()
                for provider in providers.values()
                if callable(close := getattr(provider, "close", None))
            ),
            return_exceptions=True,
        )
