"""Provider implementations shipped with Merlin."""

from .anthropic_messages import AnthropicMessagesConfig, AnthropicMessagesProvider
from .capabilities import (
    BUILTIN_CATALOG,
    CapabilityClaim,
    CapabilityConflict,
    FieldProvenance,
    ModelCapabilities,
    ModelCapabilityRegistry,
    OperatorOverride,
    ResolvedField,
    RouteIdentity,
    SourceTier,
    load_last_good_snapshot,
    load_snapshot_claims,
    models_dev_catalogue,
    openrouter_catalogue,
    overrides_from_provider_config,
)
from .claude_cli import ClaudeCliConfig, ClaudeCliProvider
from .codex_app_server import CodexAppServerConfig, CodexAppServerProvider
from .gemini import GeminiConfig, GeminiProvider
from .kimi_acp import KimiAcpConfig, KimiAcpProvider
from .openai_compatible import (
    OpenAICompatibleProvider,
    ProviderCapabilities,
    ProviderConfig,
    ProviderError,
    ProviderQuotaSnapshot,
)
from .openai_responses import OpenAIResponsesConfig, OpenAIResponsesProvider
from .pi_rpc import PiRpcConfig, PiRpcProvider
from .routing import ModelRoutingProvider
from .targets import TargetCredentialError, TargetProviderFactory, TargetSessionIdentity

__all__ = [
    "BUILTIN_CATALOG",
    "AnthropicMessagesConfig",
    "AnthropicMessagesProvider",
    "CapabilityClaim",
    "CapabilityConflict",
    "ClaudeCliConfig",
    "ClaudeCliProvider",
    "CodexAppServerConfig",
    "CodexAppServerProvider",
    "FieldProvenance",
    "GeminiConfig",
    "GeminiProvider",
    "KimiAcpConfig",
    "KimiAcpProvider",
    "ModelCapabilities",
    "ModelCapabilityRegistry",
    "ModelRoutingProvider",
    "OpenAICompatibleProvider",
    "OpenAIResponsesConfig",
    "OpenAIResponsesProvider",
    "OperatorOverride",
    "PiRpcConfig",
    "PiRpcProvider",
    "ProviderCapabilities",
    "ProviderConfig",
    "ProviderError",
    "ProviderQuotaSnapshot",
    "ResolvedField",
    "RouteIdentity",
    "SourceTier",
    "TargetCredentialError",
    "TargetProviderFactory",
    "TargetSessionIdentity",
    "load_last_good_snapshot",
    "load_snapshot_claims",
    "models_dev_catalogue",
    "openrouter_catalogue",
    "overrides_from_provider_config",
]
