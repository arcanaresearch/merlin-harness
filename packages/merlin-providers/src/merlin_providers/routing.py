"""Exact model-to-transport routing for heterogeneous provider deployments."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from merlin_contracts import ChatRequest, DynamicToolHandler, ModelEvent, Provider

from .capabilities import RouteIdentity

logger = logging.getLogger(__name__)
_IMAGE_INPUT_TRANSPORTS = frozenset(
    {
        "openai_compatible",
        "open_ai_compatible",
        "openai_responses",
        "anthropic_messages",
    }
)


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


def _with_route_capabilities(
    capabilities: object | None,
    model: str,
    *,
    vision: bool | None,
    context_window_tokens: int | None,
) -> _SharedProviderCapabilities:
    return _SharedProviderCapabilities(
        streaming=bool(getattr(capabilities, "streaming", False)),
        tools=bool(getattr(capabilities, "tools", False)),
        usage_in_stream=bool(getattr(capabilities, "usage_in_stream", False)),
        model=model,
        vision=vision,
        context_window_tokens=context_window_tokens,
    )


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
        *,
        request_efforts: Mapping[str, str] | None = None,
        request_output_tokens: Mapping[str, int] | None = None,
    ) -> None:
        if any(not model.strip() for model in routes):
            raise ValueError("provider route model names must not be empty")
        self.default = default
        self.routes = dict(routes)
        self.capability_registry = capability_registry
        self.request_efforts = dict(request_efforts or {})
        self.request_output_tokens = dict(request_output_tokens or {})

    def request_output_limit(self, workload: str | None) -> int | None:
        return self.request_output_tokens.get(workload) if workload is not None else None

    def output_limit_capability(self, model: str) -> str:
        """Describe enforcement honestly: provider-native, local truncation, or unsupported."""
        identity = self.route_identity_for_model(model)
        transport = str(getattr(identity, "transport", "openai_compatible"))
        if transport in {
            "openai_compatible",
            "open_ai_compatible",
            "openai_responses",
            "anthropic_messages",
            "gemini",
        }:
            return "native"
        if transport in {"claude_cli", "kimi_acp"}:
            return "truncation"
        return "unsupported"

    def resolve_request(self, request: ChatRequest) -> ChatRequest:
        """Resolve workload controls once; callers may then budget, digest, and transport it."""
        if request.workload is None:
            return request
        updates: dict[str, object] = {}
        limit = self.request_output_limit(request.workload)
        if limit is not None and self.output_limit_capability(request.model) != "native":
            raise ValueError(
                f"transport for model {request.model!r} cannot enforce request output limits "
                "before generation"
            )
        if limit is not None and (
            request.max_output_tokens is None or request.max_output_tokens > limit
        ):
            updates["max_output_tokens"] = limit
        effort = self.request_efforts.get(request.workload)
        if effort == "off":
            # Disabled is authoritative and no fallback effort survives it.
            updates["reasoning"] = "disabled"
            updates["reasoning_effort"] = None
            updates["reasoning_control"] = "required"
        elif effort not in {None, "default"} and request.reasoning != "disabled":
            updates["reasoning_effort"] = effort
        return request.model_copy(update=updates) if updates else request

    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        request = self.resolve_request(request)
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
        identity = self.route_identity_for_model(model)
        transport = str(getattr(identity, "transport", "openai_compatible"))
        configured_vision = getattr(capabilities, "vision", None)
        effective_vision = (
            False
            if transport not in _IMAGE_INPUT_TRANSPORTS
            else configured_vision
            if isinstance(configured_vision, bool)
            else None
        )
        window = getattr(capabilities, "context_window_tokens", None)
        if isinstance(window, int) and not isinstance(window, bool):
            if effective_vision == configured_vision:
                return capabilities
            return _with_route_capabilities(
                capabilities,
                model,
                vision=effective_vision,
                context_window_tokens=window,
            )
        resolve = getattr(self.capability_registry, "resolve_model", None)
        if not callable(resolve):
            if effective_vision != configured_vision:
                return _with_route_capabilities(
                    capabilities,
                    model,
                    vision=effective_vision,
                    context_window_tokens=None,
                )
            return capabilities
        resolved = resolve(
            model,
            transport=transport,
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
            if effective_vision != configured_vision:
                return _with_route_capabilities(
                    capabilities,
                    model,
                    vision=effective_vision,
                    context_window_tokens=None,
                )
            return capabilities
        modalities = getattr(resolved.modalities, "value", None) or ()
        vision = (
            effective_vision
            if effective_vision is not None
            else True
            if "image" in modalities
            else None
        )
        return _with_route_capabilities(
            capabilities,
            model,
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
        request = self.resolve_request(request)
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
