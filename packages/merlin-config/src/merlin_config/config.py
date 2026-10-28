from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_TELEMETRY_ENDPOINT = "https://telemetry.plunder.me/v1/logs"
_DEFAULT_JEV_URL = "https://api.typesafe.ai/v1/systemone"
_TOP_LEVEL_KEYS = {
    "browser",
    "database",
    "evaluation",
    "judgments",
    "max_coverage_extensions",
    "mcp",
    "permission_mode",
    "provider",
    "runtime",
    "specialist_composition",
    "telemetry",
}
_ENV_OVERRIDES = {
    "MERLIN_DATABASE_URL": ("database", "url"),
    "MERLIN_MAX_COVERAGE_EXTENSIONS": ("max_coverage_extensions",),
    "MERLIN_PROVIDER_BASE_URL": ("provider", "base_url"),
    "MERLIN_PROVIDER_MODEL": ("provider", "model"),
    "MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER": ("provider", "output_token_parameter"),
    "MERLIN_PERMISSION_MODE": ("permission_mode",),
    "MERLIN_TELEMETRY_ENABLED": ("telemetry", "enabled"),
    "MERLIN_TELEMETRY_ENDPOINT": ("telemetry", "endpoint"),
    "MERLIN_TELEMETRY_TOKEN": ("telemetry", "token"),
    "MERLIN_TELEMETRY_SERVICE_NAME": ("telemetry", "service_name"),
    "MERLIN_TELEMETRY_CLIENT": ("telemetry", "client"),
    "MERLIN_JUDGMENTS_LAYA_URL": ("judgments", "laya_url"),
    "MERLIN_JUDGMENTS_LAYA_TOKEN": ("judgments", "laya_token"),
    "MERLIN_JUDGMENTS_JEV_URL": ("judgments", "jev_url"),
    "MERLIN_JUDGMENTS_JEV_TOKEN": ("judgments", "jev_token"),
    "MERLIN_JUDGMENTS_JEV_DECISIONS": ("judgments", "jev_decisions_enabled"),
}
_SECRET_WORDS = ("api_key", "authorization", "credential", "password", "secret", "token")
# Settings a running daemon may apply without restart. Model/route selection is
# reload-safe: the kernel re-resolves its route and per-route capability window
# at every request, and new campaigns re-resolve role routes at admission.
_RELOAD_SAFE = {
    ("max_coverage_extensions",),
    ("permission_mode",),
    ("specialist_composition",),
    ("provider", "model"),
    ("provider", "role_models"),
    ("provider", "model_transports"),
    ("provider", "context_window_tokens"),
    ("provider", "model_capabilities"),
}
_PROVIDER_REQUEST_WORKLOADS = {
    "lead",
    "worker",
    "recon",
    "validator",
    "planner",
    "judge",
    "assessment_attacker",
    "assessment_target",
}
_PROVIDER_PROFILE_ROUTES = {
    "auth-session",
    "injection",
    "data-exposure",
    "business-logic",
    "capability-abuse",
    "adversarial-explorer",
    "adversarial-researcher",
    "novelty-critic",
    "experiment-planner",
    "independent-validator",
    "remediation-specialist",
}
_PROVIDER_TRANSPORTS = {
    "anthropic_messages",
    "claude_cli",
    "codex_app_server",
    "gemini",
    "kimi_acp",
    "openai_compatible",
    "openai_responses",
}


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    directory: Path

    @classmethod
    def default(cls) -> ConfigPaths:
        preferred = Path.home() / ".merlin"
        legacy = Path.home() / ".redpen-python"
        # Key on the config file, not the directory: runtime scaffolding
        # (logs, daemon.lock, telemetry) may create ~/.merlin before the
        # first config read on an upgraded install.
        if not (preferred / "config.toml").exists() and (legacy / "config.toml").exists():
            return cls(legacy)
        return cls(preferred)

    @property
    def config(self) -> Path:
        return self.directory / "config.toml"

    @property
    def credentials(self) -> Path:
        return self.directory / "credentials.json"


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str | None = None
    values: Mapping[str, object] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def specialist_composition(self) -> str:
        """Return the specialist composition policy, independent of coverage profile."""
        return str(self.values.get("specialist_composition", "opinionated"))

    @property
    def max_coverage_extensions(self) -> int | None:
        """Cap on operator-granted coverage extensions; None means ask every time."""
        value = self.values.get("max_coverage_extensions")
        if value is None:
            return None
        return int(value)  # type: ignore[arg-type]

    @property
    def runtime(self) -> RuntimeTuning:
        value = self.values.get("runtime", {})
        assert isinstance(value, Mapping)
        return RuntimeTuning(
            agent_lease_seconds=float(value.get("agent_lease_seconds", 120)),
            agent_max_steps_per_turn=_optional_int(value.get("agent_max_steps_per_turn")),
            agent_max_result_repairs=int(value.get("agent_max_result_repairs", 2)),
            cleanup_max_attempts=int(value.get("cleanup_max_attempts", 3)),
            agent_browser_observation_soft_limit=int(
                value.get("agent_browser_observation_soft_limit", 24)
            ),
            agent_max_target_exchanges=int(value.get("agent_max_target_exchanges", 8)),
            agent_context_compaction_trigger_ratio=float(
                value.get("agent_context_compaction_trigger_ratio", 0.8)
            ),
            agent_context_compaction_target_ratio=float(
                value.get("agent_context_compaction_target_ratio", 0.5)
            ),
            agent_context_fallback_tokens=int(value.get("agent_context_fallback_tokens", 131_072)),
            agent_compact_exchange_window=int(value.get("agent_compact_exchange_window", 6)),
            campaign_stall_seconds=float(value.get("campaign_stall_seconds", 360)),
            campaign_hypothesis_budget=int(value.get("campaign_hypothesis_budget", 100)),
            campaign_language_escalation_budget=int(
                value.get("campaign_language_escalation_budget", 3)
            ),
            campaign_lead_assessment_batch_size=int(
                value.get("campaign_lead_assessment_batch_size", 4)
            ),
            campaign_lead_assessment_batch_seconds=float(
                value.get("campaign_lead_assessment_batch_seconds", 240)
            ),
            campaign_transport_retry_backoff_seconds=float(
                value.get("campaign_transport_retry_backoff_seconds", 15)
            ),
            campaign_transport_retry_backoff_max_seconds=float(
                value.get("campaign_transport_retry_backoff_max_seconds", 120)
            ),
            campaign_max_turns=_optional_int(value.get("campaign_max_turns")),
            campaign_max_tokens=_optional_int(value.get("campaign_max_tokens")),
            campaign_max_seconds=_optional_float(value.get("campaign_max_seconds")),
            campaign_preflight_max_tokens=_optional_int(value.get("campaign_preflight_max_tokens")),
            campaign_preflight_max_cost_usd=_optional_float(
                value.get("campaign_preflight_max_cost_usd")
            ),
            knowledge_entry_limit=int(value.get("knowledge_entry_limit", 12)),
            learning_evidence_validity_seconds=int(
                value.get("learning_evidence_validity_seconds", 86_400)
            ),
            local_control_max_stream_events=int(
                value.get("local_control_max_stream_events", 65_536)
            ),
            local_control_stream_journal_batch=int(
                value.get("local_control_stream_journal_batch", 512)
            ),
            local_control_request_timeout_seconds=float(
                value.get("local_control_request_timeout_seconds", 30)
            ),
            operational_log_retention_entries=int(
                value.get("operational_log_retention_entries", 10_000)
            ),
            operational_log_retention_days=int(value.get("operational_log_retention_days", 30)),
            config_reload_poll_seconds=float(value.get("config_reload_poll_seconds", 1)),
            tool_read_max_bytes=int(value.get("tool_read_max_bytes", 262_144)),
            tool_action_timeout_seconds=float(value.get("tool_action_timeout_seconds", 120)),
            tool_shell_timeout_seconds=float(value.get("tool_shell_timeout_seconds", 30)),
            tool_shell_max_output_bytes=int(value.get("tool_shell_max_output_bytes", 262_144)),
            tool_shell_termination_grace_seconds=float(
                value.get("tool_shell_termination_grace_seconds", 1)
            ),
            daemon_startup_seconds=float(value.get("daemon_startup_seconds", 180)),
            daemon_shutdown_seconds=float(value.get("daemon_shutdown_seconds", 30)),
        )

    @property
    def provider_runtime(self) -> ProviderRuntimeTuning:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        return ProviderRuntimeTuning(
            connect_timeout_seconds=float(value.get("connect_timeout_seconds", 10)),
            idle_timeout_seconds=float(value.get("idle_timeout_seconds", 60)),
            total_timeout_seconds=float(value.get("total_timeout_seconds", 1_800)),
            max_attempts=int(value.get("max_attempts", 3)),
            max_concurrent_requests=int(value.get("max_concurrent_requests", 8)),
            retry_base_seconds=float(value.get("retry_base_seconds", 0.25)),
            retry_max_seconds=float(value.get("retry_max_seconds", 10)),
            max_tool_calls=int(value.get("max_tool_calls", 32)),
            max_tool_argument_bytes=int(value.get("max_tool_argument_bytes", 262_144)),
            max_tool_argument_depth=int(value.get("max_tool_argument_depth", 32)),
            worker_retry_attempts=int(value.get("worker_retry_attempts", 3)),
            worker_retry_backoff_seconds=float(value.get("worker_retry_backoff_seconds", 1)),
            vision=bool(value.get("vision", False)),
            context_window_tokens=_optional_int(value.get("context_window_tokens")),
            input_cost_per_million=_optional_float(value.get("input_cost_per_million")),
            output_cost_per_million=_optional_float(value.get("output_cost_per_million")),
            cache_read_input_cost_per_million=_optional_float(
                value.get("cache_read_input_cost_per_million")
            ),
            output_token_parameter=str(
                value.get("output_token_parameter", "max_completion_tokens")
            ),
            reasoning_parameter=str(value.get("reasoning_parameter", "none")),
            effort=str(value.get("effort", "default")),
            requires_reasoning_content_for_tool_calls=(
                value.get("requires_reasoning_content_for_tool_calls")
                if isinstance(value.get("requires_reasoning_content_for_tool_calls"), bool)
                else None
            ),
        )

    @property
    def provider_role_models(self) -> Mapping[str, str]:
        """Return operator-evaluated role/profile model routes.

        Missing routes deliberately preserve homogeneous single-model behavior.
        """
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        routes = value.get("role_models", {})
        assert isinstance(routes, Mapping)
        return MappingProxyType({str(key): str(model) for key, model in routes.items()})

    @property
    def provider_model_efforts(self) -> Mapping[str, str]:
        """Return per-model reasoning-effort overrides keyed by model ID.

        Missing entries deliberately fall back to the global provider effort,
        preserving homogeneous single-effort behavior.
        """
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        efforts = value.get("model_efforts", {})
        assert isinstance(efforts, Mapping)
        return MappingProxyType({str(model): str(effort) for model, effort in efforts.items()})

    @property
    def provider_request_efforts(self) -> Mapping[str, str]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        efforts = value.get("request_efforts", {})
        assert isinstance(efforts, Mapping)
        return MappingProxyType(
            {str(workload): str(effort) for workload, effort in efforts.items()}
        )

    @property
    def provider_request_output_tokens(self) -> Mapping[str, int]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        limits = value.get("request_output_tokens", {})
        assert isinstance(limits, Mapping)
        return MappingProxyType({str(workload): int(limit) for workload, limit in limits.items()})

    @property
    def provider_model_transports(self) -> Mapping[str, str]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        routes = value.get("model_transports", {})
        assert isinstance(routes, Mapping)
        return MappingProxyType({str(model): str(route) for model, route in routes.items()})

    @property
    def provider_model_endpoints(self) -> Mapping[str, Mapping[str, object]]:
        """Return per-model endpoint overrides (base_url and optional api_key).

        Missing entries deliberately fall back to the shared [provider] endpoint
        and credential, preserving homogeneous single-provider behavior.
        """
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        endpoints = value.get("model_endpoints", {})
        assert isinstance(endpoints, Mapping)
        return MappingProxyType(
            {
                str(model): MappingProxyType({str(k): v for k, v in endpoint.items()})
                for model, endpoint in endpoints.items()
                if isinstance(endpoint, Mapping)
            }
        )

    @property
    def provider_model_pricing(self) -> Mapping[str, tuple[float, float]]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        pricing = value.get("model_pricing", {})
        assert isinstance(pricing, Mapping)
        return MappingProxyType(
            {
                str(model): (
                    float(rates["input_cost_per_million"]),
                    float(rates["output_cost_per_million"]),
                )
                for model, rates in pricing.items()
                if isinstance(rates, Mapping)
            }
        )

    @property
    def provider_model_cache_read_pricing(self) -> Mapping[str, float | None]:
        """Optional per-model cache-read rates (USD per million tokens).

        ``None`` means the model is priced but its cache-read rate is unknown;
        cost estimation then bills cache reads at the full input rate.
        """
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        pricing = value.get("model_pricing", {})
        assert isinstance(pricing, Mapping)
        return MappingProxyType(
            {
                str(model): (
                    None
                    if rates.get("cache_read_input_cost_per_million") is None
                    else float(rates["cache_read_input_cost_per_million"])
                )
                for model, rates in pricing.items()
                if isinstance(rates, Mapping)
            }
        )

    @property
    def provider_model_capabilities(self) -> Mapping[str, tuple[str, ...]]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        configured = value.get("model_capabilities", {})
        assert isinstance(configured, Mapping)
        return MappingProxyType(
            {
                str(model): tuple(str(capability) for capability in capabilities)
                for model, capabilities in configured.items()
                if isinstance(capabilities, list)
            }
        )

    @property
    def provider_escalation_models(self) -> Mapping[str, tuple[str, ...]]:
        value = self.values.get("provider", {})
        assert isinstance(value, Mapping)
        configured = value.get("escalation_models", {})
        assert isinstance(configured, Mapping)
        return MappingProxyType(
            {
                str(role): tuple(str(model) for model in models)
                for role, models in configured.items()
                if isinstance(models, list)
            }
        )

    @property
    def evaluation_suite_files(self) -> tuple[Path, ...]:
        value = self.values.get("evaluation", {})
        assert isinstance(value, Mapping)
        configured = value.get("suite_files", [])
        assert isinstance(configured, list)
        return tuple(Path(str(path)) for path in configured)

    @property
    def mcp_runtime(self) -> McpRuntimeTuning:
        value = self.values.get("mcp", {})
        assert isinstance(value, Mapping)
        return McpRuntimeTuning(
            startup_timeout_seconds=float(value.get("startup_timeout_seconds", 10)),
            invocation_timeout_seconds=float(value.get("invocation_timeout_seconds", 30)),
            output_timeout_seconds=float(value.get("output_timeout_seconds", 5)),
            shutdown_timeout_seconds=float(value.get("shutdown_timeout_seconds", 5)),
            connect_timeout_seconds=float(value.get("connect_timeout_seconds", 10)),
            health_timeout_seconds=float(value.get("health_timeout_seconds", 5)),
            reconnect_attempts=int(value.get("reconnect_attempts", 1)),
            reconnect_delay_seconds=float(value.get("reconnect_delay_seconds", 0.1)),
            max_tools=int(value.get("max_tools", 128)),
            max_schema_bytes=int(value.get("max_schema_bytes", 256_000)),
            max_output_bytes=int(value.get("max_output_bytes", 1_000_000)),
        )

    @property
    def telemetry(self) -> TelemetryConfig:
        value = self.values.get("telemetry", {})
        assert isinstance(value, Mapping)
        enabled = value.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().casefold() in {"1", "true", "yes", "on"}
        endpoint = value.get("endpoint", _DEFAULT_TELEMETRY_ENDPOINT)
        token = value.get("token")
        client = value.get("client")
        return TelemetryConfig(
            enabled=bool(enabled),
            endpoint=None if endpoint is None else str(endpoint),
            token=None if token is None else str(token),
            service_name=str(value.get("service_name", "merlin-harness")),
            client=None if client is None else str(client),
        )

    @property
    def judgments(self) -> JudgmentsConfig:
        value = self.values.get("judgments", {})
        assert isinstance(value, Mapping)
        laya_url = value.get("laya_url")
        laya_token = value.get("laya_token")
        jev_token = value.get("jev_token")
        jev_decisions = value.get("jev_decisions_enabled", True)
        if isinstance(jev_decisions, str):
            jev_decisions = jev_decisions.strip().casefold() in {"1", "true", "yes", "on"}
        return JudgmentsConfig(
            laya_url=None if laya_url is None else str(laya_url),
            laya_token=None if laya_token is None else str(laya_token),
            jev_url=str(value.get("jev_url", _DEFAULT_JEV_URL)),
            jev_token=None if jev_token is None else str(jev_token),
            jev_decisions_enabled=bool(jev_decisions),
            timeout_seconds=float(value.get("timeout_seconds", 30.0)),
        )

    def redacted(self) -> Mapping[str, object]:
        return _redact(self.values)

    def diagnostics(self) -> Mapping[str, object]:
        """Return effective non-secret values plus attributable configuration sources."""
        return {
            "configured": self.redacted(),
            "sources": dict(sorted(self.sources.items())),
            "effective": {
                "runtime": asdict(self.runtime),
                "provider_runtime": asdict(self.provider_runtime),
                "mcp_runtime": asdict(self.mcp_runtime),
                "telemetry": {
                    "enabled": self.telemetry.enabled,
                    "endpoint": self.telemetry.endpoint,
                    "service_name": self.telemetry.service_name,
                    "client": self.telemetry.client,
                },
                "specialist_composition": self.specialist_composition,
                "max_coverage_extensions": self.max_coverage_extensions,
            },
            "reload_safe": sorted(".".join(path) for path in _RELOAD_SAFE),
        }

    def changed_settings(self, newer: Config) -> tuple[frozenset[str], frozenset[str]]:
        """Return (reloadable, restart-required) dotted setting names.

        A change nested under a reload-safe subtree (for example
        ``provider.role_models.<role>``) is reported at the reload-safe prefix
        so the daemon applies the whole route table atomically.
        """
        changed = _changed(self.values, newer.values)
        reloadable: set[str] = set()
        restart: set[str] = set()
        for path in changed:
            unit = next(
                (
                    prefix
                    for length in range(len(path), 0, -1)
                    if (prefix := path[:length]) in _RELOAD_SAFE
                ),
                None,
            )
            (reloadable if unit is not None else restart).add(
                ".".join(unit) if unit is not None else ".".join(path)
            )
        return frozenset(reloadable), frozenset(restart)


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """OpenTelemetry log export for target exchange attempts/replies; on by default."""

    enabled: bool = True
    endpoint: str | None = _DEFAULT_TELEMETRY_ENDPOINT
    token: str | None = None
    service_name: str = "merlin-harness"
    client: str | None = None


@dataclass(frozen=True, slots=True)
class JudgmentsConfig:
    """Advisory semantic judgment routing.

    When ``jev_token`` is set, refusal grading routes to the TypeSafe Jev
    typed-decision API at ``jev_url``; else when ``laya_url`` and
    ``laya_token`` are set, to the self-hosted Laya decision model; otherwise
    the caller falls back to a chat-model judge, then the deterministic marker.
    """

    laya_url: str | None = None
    laya_token: str | None = None
    jev_url: str = _DEFAULT_JEV_URL
    jev_token: str | None = None
    # Master toggle for Jev decision seams: False forces LLM/deterministic
    # decisions even when ``jev_token`` is set.
    jev_decisions_enabled: bool = True
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RuntimeTuning:
    # Must comfortably exceed one slow provider turn (reasoning models take
    # 60-120s); the lease is a dead-worker detector, not a liveness poll.
    agent_lease_seconds: float = 120
    # None leaves termination to evidence-based progress guards and explicit
    # campaign/provider constraints. A configured value remains an operator cap.
    agent_max_steps_per_turn: int | None = None
    agent_max_result_repairs: int = 2
    cleanup_max_attempts: int = 3
    agent_browser_observation_soft_limit: int = 24
    # Per-attempt target-contact turn cap applied when an assignment carries no
    # reviewed interaction bound; reaching it finalizes with the evidence
    # already collected and records a "turn budget exhausted" limitation.
    agent_max_target_exchanges: int = 8
    agent_context_compaction_trigger_ratio: float = 0.8
    agent_context_compaction_target_ratio: float = 0.5
    agent_context_fallback_tokens: int = 131_072
    # Per-turn verbatim transcript window for direct/baseline workers: only the
    # last N target exchanges are re-sent to the provider; older exchanges
    # remain durable as receipts plus the compacted history digest. 0 disables
    # the window and restores pressure-only compaction.
    agent_compact_exchange_window: int = 6
    campaign_stall_seconds: float = 360
    campaign_hypothesis_budget: int = 100
    campaign_language_escalation_budget: int = 3
    campaign_lead_assessment_batch_size: int = 4
    campaign_lead_assessment_batch_seconds: float = 240
    # Pacing between transport_transient retry generations: base interval
    # doubled per generation up to the ceiling, ±25% jitter. 0 disables pacing.
    campaign_transport_retry_backoff_seconds: float = 15
    campaign_transport_retry_backoff_max_seconds: float = 120
    campaign_max_turns: int | None = None
    campaign_max_tokens: int | None = None
    campaign_max_seconds: float | None = None
    # Preflight hard caps: execution is refused when the conservative (2x)
    # pre-spend estimate exceeds them, unless the operator overrides. None
    # falls back to the reviewed manifest budget caps.
    campaign_preflight_max_tokens: int | None = None
    campaign_preflight_max_cost_usd: float | None = None
    knowledge_entry_limit: int = 12
    learning_evidence_validity_seconds: int = 86_400
    local_control_max_stream_events: int = 65_536
    local_control_stream_journal_batch: int = 512
    local_control_request_timeout_seconds: float = 30
    operational_log_retention_entries: int = 10_000
    operational_log_retention_days: int = 30
    config_reload_poll_seconds: float = 1
    tool_read_max_bytes: int = 262_144
    tool_action_timeout_seconds: float = 120
    tool_shell_timeout_seconds: float = 30
    tool_shell_max_output_bytes: int = 262_144
    tool_shell_termination_grace_seconds: float = 1
    daemon_startup_seconds: float = 180
    daemon_shutdown_seconds: float = 30


@dataclass(frozen=True, slots=True)
class ProviderRuntimeTuning:
    connect_timeout_seconds: float = 10
    idle_timeout_seconds: float = 60
    # Agentic provider turns can include many bounded Merlin tool calls. Keep the
    # longer absolute ceiling while failing a silent transport promptly.
    total_timeout_seconds: float = 1_800
    max_attempts: int = 3
    max_concurrent_requests: int = 8
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 10
    max_tool_calls: int = 32
    max_tool_argument_bytes: int = 262_144
    max_tool_argument_depth: int = 32
    worker_retry_attempts: int = 3
    worker_retry_backoff_seconds: float = 1
    vision: bool = False
    context_window_tokens: int | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    cache_read_input_cost_per_million: float | None = None
    output_token_parameter: str = "max_completion_tokens"
    reasoning_parameter: str = "none"
    effort: str = "default"
    requires_reasoning_content_for_tool_calls: bool | None = None


@dataclass(frozen=True, slots=True)
class McpRuntimeTuning:
    startup_timeout_seconds: float = 10
    invocation_timeout_seconds: float = 30
    output_timeout_seconds: float = 5
    shutdown_timeout_seconds: float = 5
    connect_timeout_seconds: float = 10
    health_timeout_seconds: float = 5
    reconnect_attempts: int = 1
    reconnect_delay_seconds: float = 0.1
    max_tools: int = 128
    max_schema_bytes: int = 256_000
    max_output_bytes: int = 1_000_000


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def load_config(
    paths: ConfigPaths | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
) -> Config:
    """Load defaults < TOML < supported environment < explicit CLI overrides."""
    paths = paths or ConfigPaths.default()
    environ = os.environ if environ is None else environ
    values: dict[str, object] = {}

    if paths.config.exists():
        _require_directory(paths.directory)
        _require_owner_only(paths.config)
        with paths.config.open("rb") as config_file:
            values = tomllib.load(config_file)

    unknown = sorted(set(values) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"unknown configuration setting(s): {', '.join(unknown)}")
    merged = _copy_mapping(values)
    sources = {".".join(path): "file" for path in _leaf_paths(merged)}
    for variable, path in _ENV_OVERRIDES.items():
        if variable in environ:
            _set_path(merged, path, environ[variable])
            sources[".".join(path)] = f"environment:{variable}"
    for dotted, value in (cli_overrides or {}).items():
        if not isinstance(dotted, str) or not dotted:
            raise ValueError("CLI configuration keys must be non-empty dotted strings")
        path = tuple(dotted.split("."))
        if path[0] not in _TOP_LEVEL_KEYS:
            raise ValueError(f"unknown configuration setting: {dotted}")
        _set_path(merged, path, value)
        sources[dotted] = "cli"
    _validate(merged)
    database = merged.get("database", {})
    database_url = database.get("url") if isinstance(database, dict) else None
    if database_url is not None and not isinstance(database_url, str):
        raise TypeError("database.url must be a string")
    return Config(database_url=database_url, values=merged, sources=sources)


def _copy_mapping(value: Mapping[str, object]) -> dict[str, Any]:
    return {
        str(key): _copy_mapping(item) if isinstance(item, Mapping) else item
        for key, item in value.items()
    }


def _leaf_paths(
    value: Mapping[str, object], prefix: tuple[str, ...] = ()
) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []
    for key, item in value.items():
        path = (*prefix, str(key))
        if isinstance(item, Mapping):
            paths.extend(_leaf_paths(item, path))
        else:
            paths.append(path)
    return tuple(paths)


def _set_path(values: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    current = values
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"configuration setting {'.'.join(path[:-1])} must be a table")
        current = child
    current[path[-1]] = value


def _validate(values: Mapping[str, object]) -> None:
    for table in (
        "provider",
        "database",
        "browser",
        "evaluation",
        "mcp",
        "runtime",
        "telemetry",
        "judgments",
    ):
        if table in values and not isinstance(values[table], Mapping):
            raise TypeError(f"{table} must be a table")
    telemetry = values.get("telemetry", {})
    if isinstance(telemetry, Mapping):
        unknown_telemetry = set(telemetry) - {
            "enabled",
            "endpoint",
            "token",
            "service_name",
            "client",
        }
        if unknown_telemetry:
            raise ValueError(
                f"unknown telemetry setting(s): {', '.join(sorted(unknown_telemetry))}"
            )
        enabled = telemetry.get("enabled", False)
        truthy = {"0", "1", "true", "false", "yes", "no", "on", "off"}
        if not isinstance(enabled, bool) and (
            not isinstance(enabled, str) or enabled.strip().casefold() not in truthy
        ):
            raise TypeError("telemetry.enabled must be a boolean")
        for key in ("endpoint", "token", "service_name", "client"):
            item = telemetry.get(key)
            if item is not None and (not isinstance(item, str) or not item.strip()):
                raise TypeError(f"telemetry.{key} must be a non-empty string")
    mode = values.get("permission_mode", "auto")
    if mode not in {"prompt", "auto", "yolo"}:
        raise ValueError("permission_mode must be 'prompt', 'auto', or 'yolo'")
    composition = values.get("specialist_composition", "opinionated")
    if composition not in {"opinionated", "autonomous"}:
        raise ValueError("specialist_composition must be 'opinionated' or 'autonomous'")
    extensions = values.get("max_coverage_extensions")
    if extensions is not None:
        if isinstance(extensions, bool) or not isinstance(extensions, (int, str)):
            raise ValueError("max_coverage_extensions must be a positive integer")
        try:
            parsed_extensions = int(extensions)
        except ValueError:
            raise ValueError("max_coverage_extensions must be a positive integer") from None
        if parsed_extensions < 1:
            raise ValueError("max_coverage_extensions must be a positive integer")
    provider = values.get("provider", {})
    if isinstance(provider, Mapping):
        unknown = set(provider) - {
            "base_url",
            "model",
            "connect_timeout_seconds",
            "idle_timeout_seconds",
            "total_timeout_seconds",
            "max_attempts",
            "max_concurrent_requests",
            "retry_base_seconds",
            "retry_max_seconds",
            "max_tool_calls",
            "max_tool_argument_bytes",
            "max_tool_argument_depth",
            "worker_retry_attempts",
            "worker_retry_backoff_seconds",
            "vision",
            "context_window_tokens",
            "input_cost_per_million",
            "output_cost_per_million",
            "cache_read_input_cost_per_million",
            "output_token_parameter",
            "reasoning_parameter",
            "requires_reasoning_content_for_tool_calls",
            "effort",
            "role_models",
            "model_efforts",
            "request_efforts",
            "request_output_tokens",
            "model_transports",
            "model_endpoints",
            "model_pricing",
            "model_capabilities",
            "escalation_models",
        }
        if unknown:
            raise ValueError(f"unknown provider setting(s): {', '.join(sorted(unknown))}")
        for key in ("base_url", "model"):
            value = provider.get(key)
            invalid = not isinstance(value, str) or not value.strip()
            if key in provider and invalid:
                raise TypeError(f"provider.{key} must be a non-empty string")
        if provider.get("output_token_parameter", "max_completion_tokens") not in {
            "max_completion_tokens",
            "max_tokens",
        }:
            raise ValueError(
                "provider.output_token_parameter must be 'max_completion_tokens' or 'max_tokens'"
            )
        if provider.get("reasoning_parameter", "none") not in {"none", "thinking"}:
            raise ValueError("provider.reasoning_parameter must be 'none' or 'thinking'")
        replay_requirement = provider.get("requires_reasoning_content_for_tool_calls")
        if replay_requirement is not None and not isinstance(replay_requirement, bool):
            raise TypeError("provider.requires_reasoning_content_for_tool_calls must be a boolean")
        if provider.get("effort", "default") not in {
            "default",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError(
                "provider.effort must be default, minimal, low, medium, high, or xhigh"
            )
        role_models = provider.get("role_models", {})
        if not isinstance(role_models, Mapping):
            raise TypeError("provider.role_models must be a table")
        allowed_roles = {"lead", "recon", "routine", "validator", *_PROVIDER_PROFILE_ROUTES}
        unknown_roles = set(role_models) - allowed_roles
        if unknown_roles:
            raise ValueError("unknown provider role model(s): " + ", ".join(sorted(unknown_roles)))
        for role, model in role_models.items():
            if not isinstance(model, str) or not model.strip():
                raise TypeError(f"provider.role_models.{role} must be a non-empty string")
        model_efforts = provider.get("model_efforts", {})
        if not isinstance(model_efforts, Mapping):
            raise TypeError("provider.model_efforts must be a table")
        allowed_efforts = {"default", "minimal", "low", "medium", "high", "xhigh"}
        for model, effort in model_efforts.items():
            if not isinstance(model, str) or not model.strip():
                raise TypeError("provider.model_efforts keys must be non-empty model IDs")
            if effort not in allowed_efforts:
                raise ValueError(
                    f"provider.model_efforts.{model} must be default, minimal, low, "
                    "medium, high, or xhigh"
                )
        request_efforts = provider.get("request_efforts", {})
        if not isinstance(request_efforts, Mapping):
            raise TypeError("provider.request_efforts must be a table")
        unknown_workloads = set(request_efforts) - _PROVIDER_REQUEST_WORKLOADS
        if unknown_workloads:
            raise ValueError(
                "unknown provider request workload(s): " + ", ".join(sorted(unknown_workloads))
            )
        for workload, effort in request_efforts.items():
            if effort not in allowed_efforts | {"off"}:
                raise ValueError(f"provider.request_efforts.{workload} has an unsupported effort")
        request_output_tokens = provider.get("request_output_tokens", {})
        if not isinstance(request_output_tokens, Mapping):
            raise TypeError("provider.request_output_tokens must be a table")
        unknown_workloads = set(request_output_tokens) - _PROVIDER_REQUEST_WORKLOADS
        if unknown_workloads:
            raise ValueError(
                "unknown provider request workload(s): " + ", ".join(sorted(unknown_workloads))
            )
        for workload, limit in request_output_tokens.items():
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError(f"provider.request_output_tokens.{workload} must be an integer")
            if not 1 <= limit <= 1_000_000:
                raise ValueError(
                    f"provider.request_output_tokens.{workload} must be between 1 and 1000000"
                )
        model_transports = provider.get("model_transports", {})
        if not isinstance(model_transports, Mapping):
            raise TypeError("provider.model_transports must be a table")
        for routed_model, transport in model_transports.items():
            if not isinstance(routed_model, str) or not routed_model.strip():
                raise TypeError("provider.model_transports model names must be non-empty strings")
            if not isinstance(transport, str):
                raise TypeError(
                    f"provider.model_transports.{routed_model} must be a transport string; "
                    "quote model IDs containing dots in TOML"
                )
            if transport not in _PROVIDER_TRANSPORTS:
                raise ValueError(
                    f"provider.model_transports.{routed_model} must be one of "
                    + ", ".join(sorted(_PROVIDER_TRANSPORTS))
                )
        model_endpoints = provider.get("model_endpoints", {})
        if not isinstance(model_endpoints, Mapping):
            raise TypeError("provider.model_endpoints must be a table")
        for endpoint_model, endpoint in model_endpoints.items():
            if not isinstance(endpoint_model, str) or not endpoint_model.strip():
                raise TypeError("provider.model_endpoints model names must be non-empty strings")
            if not isinstance(endpoint, Mapping):
                raise TypeError(f"provider.model_endpoints.{endpoint_model} must be a table")
            unknown_endpoint_keys = set(endpoint) - {
                "base_url",
                "api_key",
                "output_token_parameter",
                "reasoning_parameter",
                "requires_reasoning_content_for_tool_calls",
            }
            if unknown_endpoint_keys:
                raise ValueError(
                    f"unknown provider.model_endpoints.{endpoint_model} setting(s): "
                    + ", ".join(sorted(unknown_endpoint_keys))
                )
            endpoint_base = endpoint.get("base_url")
            if not isinstance(endpoint_base, str) or not endpoint_base.strip():
                raise TypeError(
                    f"provider.model_endpoints.{endpoint_model}.base_url must be a non-empty string"
                )
            endpoint_key = endpoint.get("api_key")
            if endpoint_key is not None and (
                not isinstance(endpoint_key, str) or not endpoint_key.strip()
            ):
                raise TypeError(
                    f"provider.model_endpoints.{endpoint_model}.api_key must be a non-empty string"
                )
            output_parameter = endpoint.get("output_token_parameter")
            if output_parameter is not None and output_parameter not in {
                "max_completion_tokens",
                "max_tokens",
            }:
                raise ValueError(
                    f"provider.model_endpoints.{endpoint_model}.output_token_parameter must be "
                    "'max_completion_tokens' or 'max_tokens'"
                )
            reasoning_parameter = endpoint.get("reasoning_parameter")
            if reasoning_parameter is not None and reasoning_parameter not in {"none", "thinking"}:
                raise ValueError(
                    f"provider.model_endpoints.{endpoint_model}.reasoning_parameter must be "
                    "'none' or 'thinking'"
                )
            replay_requirement = endpoint.get("requires_reasoning_content_for_tool_calls")
            if replay_requirement is not None and not isinstance(replay_requirement, bool):
                raise TypeError(
                    "provider.model_endpoints."
                    f"{endpoint_model}.requires_reasoning_content_for_tool_calls must be a boolean"
                )
        model_pricing = provider.get("model_pricing", {})
        if not isinstance(model_pricing, Mapping):
            raise TypeError("provider.model_pricing must be a table")
        for model, rates in model_pricing.items():
            if not isinstance(rates, Mapping):
                raise TypeError(f"provider.model_pricing.{model} must be a table")
            expected = {"input_cost_per_million", "output_cost_per_million"}
            allowed = expected | {"cache_read_input_cost_per_million"}
            if not expected <= set(rates) <= allowed:
                raise ValueError(f"provider.model_pricing.{model} requires input and output rates")
            for key in set(rates) & allowed:
                rate = rates[key]
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
                    raise TypeError(
                        f"provider.model_pricing.{model}.{key} must be non-negative numeric"
                    )
        model_capabilities = provider.get("model_capabilities", {})
        if not isinstance(model_capabilities, Mapping):
            raise TypeError("provider.model_capabilities must be a table")
        for model, capabilities in model_capabilities.items():
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or any(not isinstance(item, str) or not item.strip() for item in capabilities)
            ):
                raise TypeError(
                    f"provider.model_capabilities.{model} must be a non-empty string array"
                )
            if len(set(capabilities)) != len(capabilities):
                raise ValueError(f"provider.model_capabilities.{model} must not contain duplicates")
        escalation_models = provider.get("escalation_models", {})
        if not isinstance(escalation_models, Mapping):
            raise TypeError("provider.escalation_models must be a table")
        unknown_escalations = set(escalation_models) - allowed_roles
        if unknown_escalations:
            raise ValueError(
                "unknown provider escalation role(s): " + ", ".join(sorted(unknown_escalations))
            )
        for role, models in escalation_models.items():
            if (
                not isinstance(models, list)
                or not models
                or any(not isinstance(model, str) or not model.strip() for model in models)
            ):
                raise TypeError(
                    f"provider.escalation_models.{role} must be a non-empty string array"
                )
            if len(set(models)) != len(models):
                raise ValueError(f"provider.escalation_models.{role} must not contain duplicates")
        if "vision" in provider and not isinstance(provider["vision"], bool):
            raise TypeError("provider.vision must be true or false")
        provider_bounds = {
            "connect_timeout_seconds": (1, 120),
            "idle_timeout_seconds": (1, 600),
            "total_timeout_seconds": (10, 3_600),
            "max_attempts": (1, 10),
            "max_concurrent_requests": (1, 8),
            "retry_base_seconds": (0.001, 60),
            "retry_max_seconds": (0.001, 600),
            "max_tool_calls": (1, 1_024),
            "max_tool_argument_bytes": (1_024, 10_000_000),
            "max_tool_argument_depth": (1, 128),
            "worker_retry_attempts": (0, 10),
            "worker_retry_backoff_seconds": (0, 60),
            "context_window_tokens": (1_024, 10_000_000),
            "input_cost_per_million": (0, 1_000_000),
            "output_cost_per_million": (0, 1_000_000),
            "cache_read_input_cost_per_million": (0, 1_000_000),
        }
        for key, (minimum, maximum) in provider_bounds.items():
            observed = provider.get(key)
            if observed is None:
                continue
            if isinstance(observed, bool) or not isinstance(observed, (int, float)):
                raise TypeError(f"provider.{key} must be numeric")
            if not minimum <= observed <= maximum:
                raise ValueError(f"provider.{key} must be between {minimum} and {maximum}")
            if key in {
                "max_attempts",
                "max_concurrent_requests",
                "max_tool_calls",
                "max_tool_argument_bytes",
                "max_tool_argument_depth",
                "worker_retry_attempts",
            } and not isinstance(observed, int):
                raise TypeError(f"provider.{key} must be an integer")
        pricing_keys = {"input_cost_per_million", "output_cost_per_million"}
        configured_pricing = (pricing_keys | {"cache_read_input_cost_per_million"}) & set(provider)
        if configured_pricing and not pricing_keys <= configured_pricing:
            raise ValueError("provider input and output pricing rates must be configured together")
        retry_base = float(provider.get("retry_base_seconds", 0.25))
        retry_max = float(provider.get("retry_max_seconds", 10))
        if retry_max < retry_base:
            raise ValueError("provider.retry_max_seconds must not be below retry_base_seconds")
    browser = values.get("browser", {})
    if isinstance(browser, Mapping):
        unknown = set(browser) - {
            "actor_wait_seconds",
            "headless",
            "profile_directory",
            "persistent_origins",
        }
        if unknown:
            raise ValueError(f"unknown browser setting(s): {', '.join(sorted(unknown))}")
        if "headless" in browser and not isinstance(browser["headless"], bool):
            raise TypeError("browser.headless must be true or false")
        wait = browser.get("actor_wait_seconds", 30)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 600:
            raise ValueError("browser.actor_wait_seconds must be between 0 and 600")
        origins = browser.get("persistent_origins", [])
        if not isinstance(origins, list) or any(
            not isinstance(origin, str) or not origin.strip() for origin in origins
        ):
            raise TypeError("browser.persistent_origins must be a string array")
        normalized_origins: list[str] = []
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "browser.persistent_origins entries must be credential-free HTTP(S) origins"
                )
            normalized_origins.append(f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}")
        if len(set(normalized_origins)) != len(normalized_origins):
            raise ValueError("browser.persistent_origins must not contain duplicates")
        if origins and not browser.get("profile_directory"):
            raise ValueError("browser.persistent_origins requires browser.profile_directory")
    evaluation = values.get("evaluation", {})
    if isinstance(evaluation, Mapping):
        unknown = set(evaluation) - {"suite_files"}
        if unknown:
            raise ValueError(f"unknown evaluation setting(s): {', '.join(sorted(unknown))}")
        suite_files = evaluation.get("suite_files", [])
        if not isinstance(suite_files, list) or any(
            not isinstance(path, str) or not path.strip() for path in suite_files
        ):
            raise TypeError("evaluation.suite_files must be a string array")
        if len(set(suite_files)) != len(suite_files):
            raise ValueError("evaluation.suite_files must not contain duplicates")
        if any(not Path(path).is_absolute() for path in suite_files):
            raise ValueError("evaluation.suite_files paths must be absolute")
    mcp = values.get("mcp", {})
    if isinstance(mcp, Mapping):
        allowed = {
            "servers",
            "startup_timeout_seconds",
            "invocation_timeout_seconds",
            "output_timeout_seconds",
            "shutdown_timeout_seconds",
            "connect_timeout_seconds",
            "health_timeout_seconds",
            "reconnect_attempts",
            "reconnect_delay_seconds",
            "max_tools",
            "max_schema_bytes",
            "max_output_bytes",
        }
        unknown = set(mcp) - allowed
        if unknown:
            raise ValueError(f"unknown MCP setting(s): {', '.join(sorted(unknown))}")
        if "servers" in mcp and not isinstance(mcp["servers"], Mapping):
            raise TypeError("mcp.servers must be a table")
        bounds = {
            "startup_timeout_seconds": (0.1, 300),
            "invocation_timeout_seconds": (0.1, 3_600),
            "output_timeout_seconds": (0.1, 300),
            "shutdown_timeout_seconds": (0.1, 300),
            "connect_timeout_seconds": (0.1, 300),
            "health_timeout_seconds": (0.1, 300),
            "reconnect_attempts": (0, 3),
            "reconnect_delay_seconds": (0, 5),
            "max_tools": (1, 4_096),
            "max_schema_bytes": (1_024, 10_000_000),
            "max_output_bytes": (1_024, 100_000_000),
        }
        for key, (minimum, maximum) in bounds.items():
            observed = mcp.get(key)
            if observed is None:
                continue
            if isinstance(observed, bool) or not isinstance(observed, (int, float)):
                raise TypeError(f"mcp.{key} must be numeric")
            if not minimum <= observed <= maximum:
                raise ValueError(f"mcp.{key} must be between {minimum} and {maximum}")
            integer_keys = {
                "reconnect_attempts",
                "max_tools",
                "max_schema_bytes",
                "max_output_bytes",
            }
            if key in integer_keys and not isinstance(observed, int):
                raise TypeError(f"mcp.{key} must be an integer")
    runtime = values.get("runtime", {})
    if isinstance(runtime, Mapping):
        allowed = {
            "agent_lease_seconds",
            "agent_max_steps_per_turn",
            "agent_max_result_repairs",
            "cleanup_max_attempts",
            "agent_browser_observation_soft_limit",
            "agent_max_target_exchanges",
            "agent_context_compaction_trigger_ratio",
            "agent_context_compaction_target_ratio",
            "agent_context_fallback_tokens",
            "agent_compact_exchange_window",
            "campaign_stall_seconds",
            "campaign_hypothesis_budget",
            "campaign_language_escalation_budget",
            "campaign_lead_assessment_batch_size",
            "campaign_lead_assessment_batch_seconds",
            "campaign_transport_retry_backoff_seconds",
            "campaign_transport_retry_backoff_max_seconds",
            "campaign_max_turns",
            "campaign_max_tokens",
            "campaign_max_seconds",
            "campaign_preflight_max_tokens",
            "campaign_preflight_max_cost_usd",
            "knowledge_entry_limit",
            "learning_evidence_validity_seconds",
            "local_control_max_stream_events",
            "local_control_stream_journal_batch",
            "local_control_request_timeout_seconds",
            "operational_log_retention_entries",
            "operational_log_retention_days",
            "config_reload_poll_seconds",
            "tool_read_max_bytes",
            "tool_action_timeout_seconds",
            "tool_shell_timeout_seconds",
            "tool_shell_max_output_bytes",
            "tool_shell_termination_grace_seconds",
            "daemon_startup_seconds",
            "daemon_shutdown_seconds",
        }
        unknown = set(runtime) - allowed
        if unknown:
            raise ValueError(f"unknown runtime setting(s): {', '.join(sorted(unknown))}")
        bounds = {
            "agent_lease_seconds": (1, 600),
            "agent_max_steps_per_turn": (1, 100_000),
            "agent_max_result_repairs": (0, 10),
            "cleanup_max_attempts": (1, 10),
            "agent_browser_observation_soft_limit": (4, 1_000),
            "agent_max_target_exchanges": (1, 10_000),
            "agent_context_compaction_trigger_ratio": (0.5, 0.95),
            "agent_context_compaction_target_ratio": (0.2, 0.8),
            "agent_context_fallback_tokens": (1_024, 10_000_000),
            "agent_compact_exchange_window": (0, 10_000),
            "campaign_stall_seconds": (30, 86_400),
            "campaign_hypothesis_budget": (1, 100_000),
            "campaign_language_escalation_budget": (0, 100),
            "campaign_lead_assessment_batch_size": (1, 1_000),
            "campaign_lead_assessment_batch_seconds": (1, 86_400),
            "campaign_transport_retry_backoff_seconds": (0, 600),
            "campaign_transport_retry_backoff_max_seconds": (1, 3_600),
            "campaign_max_turns": (1, 10_000),
            "campaign_max_tokens": (1, 100_000_000),
            "campaign_max_seconds": (1, 604_800),
            "campaign_preflight_max_tokens": (1, 100_000_000),
            "campaign_preflight_max_cost_usd": (0.01, 1_000_000),
            "knowledge_entry_limit": (1, 12),
            "learning_evidence_validity_seconds": (60, 31_536_000),
            "local_control_max_stream_events": (256, 1_000_000),
            "local_control_stream_journal_batch": (1, 4_096),
            "local_control_request_timeout_seconds": (1, 300),
            "operational_log_retention_entries": (100, 1_000_000),
            "operational_log_retention_days": (1, 365),
            "config_reload_poll_seconds": (0.1, 60),
            "tool_read_max_bytes": (1_024, 100_000_000),
            "tool_action_timeout_seconds": (0.1, 3_600),
            "tool_shell_timeout_seconds": (0.1, 3_600),
            "tool_shell_max_output_bytes": (1_024, 100_000_000),
            "tool_shell_termination_grace_seconds": (0.1, 30),
            "daemon_startup_seconds": (1, 1_800),
            "daemon_shutdown_seconds": (1, 300),
        }
        for key, (minimum, maximum) in bounds.items():
            observed = runtime.get(key)
            if observed is None:
                continue
            if isinstance(observed, bool) or not isinstance(observed, (int, float)):
                raise TypeError(f"runtime.{key} must be numeric")
            if not minimum <= observed <= maximum:
                raise ValueError(f"runtime.{key} must be between {minimum} and {maximum}")
            if key not in {
                "agent_lease_seconds",
                "agent_context_compaction_trigger_ratio",
                "agent_context_compaction_target_ratio",
                "campaign_stall_seconds",
                "campaign_preflight_max_cost_usd",
                "local_control_request_timeout_seconds",
                "config_reload_poll_seconds",
                "tool_action_timeout_seconds",
                "tool_shell_timeout_seconds",
                "tool_shell_termination_grace_seconds",
                "daemon_startup_seconds",
                "daemon_shutdown_seconds",
            } and not isinstance(observed, int):
                raise TypeError(f"runtime.{key} must be an integer")
        trigger = float(runtime.get("agent_context_compaction_trigger_ratio", 0.8))
        target = float(runtime.get("agent_context_compaction_target_ratio", 0.5))
        if target >= trigger:
            raise ValueError(
                "runtime.agent_context_compaction_target_ratio must be less than "
                "runtime.agent_context_compaction_trigger_ratio"
            )


def _redact(value: object, key: str = "") -> Any:
    if any(word in key.casefold() for word in _SECRET_WORDS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _changed(old: Mapping[str, object], new: Mapping[str, object]) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()

    def walk(left: object, right: object, prefix: tuple[str, ...]) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in set(left) | set(right):
                walk(left.get(key), right.get(key), (*prefix, str(key)))
        elif left != right:
            result.add(prefix)

    walk(old, new, ())
    return result


def _require_owner_only(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must not be accessible by group or others")


def _require_directory(path: Path) -> None:
    actual = path.stat().st_mode & 0o777
    if actual != 0o700:
        raise PermissionError(f"{path} must have mode 0700, not {actual:04o}")
