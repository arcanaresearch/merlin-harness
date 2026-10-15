from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w
from merlin_config import ConfigPaths, CredentialStore, load_config

_UNSET = object()
_CLI_TRANSPORTS = {"codex_app_server", "claude_cli", "kimi_acp"}
_NATIVE_HTTP_TRANSPORTS = {"openai_responses", "anthropic_messages", "gemini"}
_MODEL_TRANSPORTS = {"openai_compatible"} | _CLI_TRANSPORTS | _NATIVE_HTTP_TRANSPORTS
_NO_CREDENTIAL_ADVICE = (
    "add the provider endpoint and key first "
    "(/setup endpoint in the TUI, or [provider.model_endpoints] in config.toml)"
)
_EFFORTS = {"default", "minimal", "low", "medium", "high", "xhigh"}
_OBVIOUS_CREDENTIAL_PLACEHOLDERS = {
    "api-key",
    "apikey",
    "change-me",
    "changeme",
    "placeholder",
    "secret",
    "test-key",
    "your-api-key",
}


def _validate_effort(effort: str) -> None:
    if effort not in _EFFORTS:
        raise ValueError("effort must be default, minimal, low, medium, high, or xhigh")


def _is_local_base_url(base_url: str) -> bool:
    return urlsplit(base_url.strip()).hostname in {"localhost", "127.0.0.1", "::1"}


def _is_obvious_credential_placeholder(api_key: str, base_url: str) -> bool:
    """Reject unmistakable setup placeholders without constraining real key formats."""
    host = (urlsplit(base_url.strip()).hostname or "").casefold()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".test"):
        return False
    return api_key.strip().casefold() in _OBVIOUS_CREDENTIAL_PLACEHOLDERS


def missing_route_credentials(
    provider: Mapping[str, object], credentials: Mapping[str, str]
) -> list[str]:
    """Return routed models whose transport cannot resolve a credential."""
    transports = provider.get("model_transports", {})
    transports = transports if isinstance(transports, Mapping) else {}
    endpoints = provider.get("model_endpoints", {})
    endpoints = endpoints if isinstance(endpoints, Mapping) else {}
    primary_model = str(provider.get("model", "")).strip()
    models = {primary_model} if primary_model else set()
    role_models = provider.get("role_models", {})
    if isinstance(role_models, Mapping):
        models.update(str(model).strip() for model in role_models.values() if str(model).strip())
    models.update(str(model).strip() for model in transports if str(model).strip())
    primary_base_url = str(provider.get("base_url", ""))
    primary_key = str(credentials.get("api_key", "")).strip()
    primary_local = _is_local_base_url(primary_base_url)
    primary_key_missing = not primary_key or _is_obvious_credential_placeholder(
        primary_key, primary_base_url
    )
    missing = []
    for model in sorted(models):
        transport = str(transports.get(model, "openai_compatible"))
        if transport in _CLI_TRANSPORTS:
            continue
        endpoint = endpoints.get(model)
        if isinstance(endpoint, Mapping):
            endpoint_base_url = str(endpoint.get("base_url", ""))
            endpoint_key = str(endpoint.get("api_key", "")).strip()
            if endpoint_key and not _is_obvious_credential_placeholder(
                endpoint_key, endpoint_base_url
            ):
                continue
            if _is_local_base_url(endpoint_base_url):
                continue
            missing.append(model)
            continue
        if transport == "openai_compatible":
            if primary_key_missing and not primary_local:
                missing.append(model)
            continue
        if transport in _NATIVE_HTTP_TRANSPORTS:
            missing.append(model)
    return missing


class ProviderConfiguration:
    """Persists provider settings using the config package's secure paths."""

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self.paths = paths or ConfigPaths.default()

    def save(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        vision: bool | None = None,
        context_window_tokens: int | None | object = _UNSET,
        output_token_parameter: str | None = None,
        reasoning_parameter: str | None = None,
        effort: str | None = None,
    ) -> None:
        if not base_url.strip() or not model.strip():
            raise ValueError("base URL and model are required")
        if api_key is not None and _is_obvious_credential_placeholder(api_key, base_url):
            raise ValueError("API key is an obvious placeholder; enter the real provider key")
        current = load_config(self.paths, environ={}).values
        values = dict(current)
        provider = values.get("provider", {})
        provider_values = dict(provider) if isinstance(provider, Mapping) else {}
        provider_values.update(base_url=base_url.strip(), model=model.strip())
        if effort is not None:
            _validate_effort(effort)
            provider_values["effort"] = effort
        if output_token_parameter is not None:
            provider_values["output_token_parameter"] = output_token_parameter
        if reasoning_parameter is not None:
            provider_values["reasoning_parameter"] = reasoning_parameter
        if vision is not None:
            provider_values["vision"] = vision
        if context_window_tokens is None:
            provider_values.pop("context_window_tokens", None)
        elif context_window_tokens is not _UNSET:
            if not isinstance(context_window_tokens, int) or context_window_tokens < 1_024:
                raise ValueError("context window must be at least 1024 tokens")
            provider_values["context_window_tokens"] = context_window_tokens
        values["provider"] = provider_values
        self._write_config(values)
        if api_key is not None:
            credentials = dict(CredentialStore(self.paths).load(environ={}))
            credentials["api_key"] = api_key
            CredentialStore(self.paths).save(credentials)

    def save_model(self, model: str) -> None:
        if not model.strip():
            raise ValueError("model is required")
        config = load_config(self.paths, environ={})
        provider = config.values.get("provider", {})
        base_url = provider.get("base_url") if isinstance(provider, Mapping) else None
        if not isinstance(base_url, str):
            raise ValueError("configure a provider with /setup first")
        # Vision support belongs to the exact model route. A model-only switch
        # must fail closed until /setup explicitly confirms the new capabilities.
        values = dict(config.values)
        provider_values = dict(provider)
        provider_values.update(base_url=base_url, model=model.strip(), vision=False)
        provider_values.pop("context_window_tokens", None)
        values["provider"] = provider_values
        self._write_config(values)

    def save_model_route(self, model: str, transport: str, *, lead_only: bool) -> None:
        model = model.strip()
        if not model:
            raise ValueError("model is required")
        if transport not in _MODEL_TRANSPORTS:
            raise ValueError("model transport is unsupported")
        if transport in {"claude_cli", "kimi_acp"} and not lead_only:
            raise ValueError("no-tool CLI transports can only be assigned to Campaign Lead")
        config = load_config(self.paths, environ={})
        provider = config.values.get("provider", {})
        if not isinstance(provider, Mapping) or not isinstance(provider.get("base_url"), str):
            raise ValueError("configure a provider with /setup first")
        values = dict(config.values)
        provider_values = dict(provider)
        if lead_only:
            role_models = provider_values.get("role_models", {})
            role_values = dict(role_models) if isinstance(role_models, Mapping) else {}
            role_values["lead"] = model
            role_values["recon"] = model
            provider_values["role_models"] = role_values
        else:
            provider_values.update(model=model, vision=False)
            provider_values.pop("context_window_tokens", None)
        routes = provider_values.get("model_transports", {})
        route_values = dict(routes) if isinstance(routes, Mapping) else {}
        if transport == "openai_compatible":
            route_values.pop(model, None)
        else:
            route_values[model] = transport
        provider_values["model_transports"] = route_values
        values["provider"] = provider_values
        self._refuse_keyless_routes(provider_values)
        self._write_config(values)

    def save_model_routing(
        self,
        worker_model: str,
        worker_transport: str,
        *,
        heterogeneous: bool,
        lead_model: str = "",
        lead_transport: str = "openai_compatible",
        recon_model: str = "",
        recon_transport: str = "openai_compatible",
        validator_model: str = "",
        validator_transport: str = "openai_compatible",
        adversarial_explorer_model: str = "",
        adversarial_explorer_transport: str = "openai_compatible",
        role_efforts: Mapping[str, str] | None = None,
        judgments_url: str = "",
        judgments_token: str = "",
        judgments_jev_token: str = "",
        judgments_jev_enabled: bool | None = None,
    ) -> None:
        """Atomically persist homogeneous or Lead/worker model routing."""
        worker_model = worker_model.strip()
        judgments_url = judgments_url.strip()
        judgments_token = judgments_token.strip()
        judgments_jev_token = judgments_jev_token.strip()
        lead_model = lead_model.strip()
        recon_model = recon_model.strip()
        validator_model = validator_model.strip()
        adversarial_explorer_model = adversarial_explorer_model.strip()
        if role_efforts is not None:
            for role_effort in role_efforts.values():
                _validate_effort(role_effort)
        if not worker_model:
            raise ValueError("Workers model is required")
        recon_explicit = bool(recon_model)
        validator_explicit = bool(validator_model)
        explorer_explicit = bool(adversarial_explorer_model)
        recon_model = recon_model or worker_model
        validator_model = validator_model or worker_model
        adversarial_explorer_model = adversarial_explorer_model or worker_model
        if (
            not {
                worker_transport,
                lead_transport,
                recon_transport,
                validator_transport,
                adversarial_explorer_transport,
            }
            <= _MODEL_TRANSPORTS
        ):
            raise ValueError("model transport is unsupported")
        if worker_transport in {"claude_cli", "kimi_acp"}:
            raise ValueError("no-tool CLI transports cannot be assigned to workers")
        if heterogeneous and not lead_model:
            raise ValueError("Campaign Lead model is required for heterogeneous routing")
        for role, transport in (
            ("workers", worker_transport),
            ("recon", recon_transport),
            ("validator", validator_transport),
            ("adversarial explorer", adversarial_explorer_transport),
        ):
            if transport in {"claude_cli", "kimi_acp"}:
                raise ValueError(f"no-tool CLI transports cannot be assigned to {role}")

        config = load_config(self.paths, environ={})
        provider = config.values.get("provider", {})
        if not isinstance(provider, Mapping) or not isinstance(provider.get("base_url"), str):
            raise ValueError("configure a provider with /setup first")
        values = dict(config.values)
        provider_values = dict(provider)
        previous_models = {str(provider_values.get("model", ""))}
        previous_roles = provider_values.get("role_models", {})
        if isinstance(previous_roles, Mapping):
            previous_models.update(str(model) for model in previous_roles.values())

        provider_values.update(model=worker_model, vision=False)
        provider_values.pop("context_window_tokens", None)
        role_values = dict(previous_roles) if isinstance(previous_roles, Mapping) else {}
        if heterogeneous:
            role_values["lead"] = lead_model
        else:
            role_values.pop("lead", None)
        if recon_explicit:
            role_values["recon"] = recon_model
        else:
            role_values.pop("recon", None)
        if validator_explicit:
            role_values["validator"] = validator_model
        else:
            role_values.pop("validator", None)
        exploration_roles = (
            "adversarial-explorer",
            "adversarial-researcher",
            "novelty-critic",
            "experiment-planner",
        )
        if explorer_explicit:
            for exploration_role in exploration_roles:
                role_values[exploration_role] = adversarial_explorer_model
        else:
            for exploration_role in exploration_roles:
                role_values.pop(exploration_role, None)
        if role_values:
            provider_values["role_models"] = role_values
        else:
            provider_values.pop("role_models", None)

        routes = provider_values.get("model_transports", {})
        route_values = dict(routes) if isinstance(routes, Mapping) else {}
        for old_model in previous_models:
            route_values.pop(old_model, None)
        selected_routes = [(worker_model, worker_transport)]
        if heterogeneous:
            selected_routes.append((lead_model, lead_transport))
        if recon_explicit:
            selected_routes.append((recon_model, recon_transport))
        if validator_explicit:
            selected_routes.append((validator_model, validator_transport))
        if explorer_explicit:
            selected_routes.append((adversarial_explorer_model, adversarial_explorer_transport))
        resolved: dict[str, str] = {}
        for routed_model, transport in selected_routes:
            previous = resolved.setdefault(routed_model, transport)
            if previous != transport:
                raise ValueError(
                    f"model {routed_model!r} cannot use both {previous} and {transport}; "
                    "choose a distinct model ID for each transport"
                )
        for routed_model, transport in resolved.items():
            # Keep the explicit default transport too: a per-model endpoint may
            # require a distinct OpenAI-compatible URL and reasoning protocol.
            route_values[routed_model] = transport
        if route_values:
            provider_values["model_transports"] = route_values
        else:
            provider_values.pop("model_transports", None)
        if role_efforts is not None:
            role_models_resolved = {
                "workers": worker_model,
                "lead": lead_model,
                "recon": recon_model,
                "validator": validator_model,
                "adversarial-explorer": adversarial_explorer_model,
            }
            effort_values = {
                str(model): str(effort)
                for model, effort in provider_values.get("model_efforts", {}).items()
                if isinstance(model, str)
            }
            for old_model in previous_models:
                effort_values.pop(old_model, None)
            for role, role_effort in role_efforts.items():
                routed_model = role_models_resolved.get(role, "")
                if not routed_model:
                    continue
                if role_effort == "default":
                    effort_values.pop(routed_model, None)
                else:
                    effort_values[routed_model] = role_effort
            if effort_values:
                provider_values["model_efforts"] = effort_values
            else:
                provider_values.pop("model_efforts", None)
        values["provider"] = provider_values
        if (
            judgments_url
            or judgments_token
            or judgments_jev_token
            or judgments_jev_enabled is not None
        ):
            existing_judgments = values.get("judgments", {})
            judgments_values = (
                dict(existing_judgments) if isinstance(existing_judgments, Mapping) else {}
            )
            if judgments_url:
                judgments_values["laya_url"] = judgments_url
            if judgments_token:
                judgments_values["laya_token"] = judgments_token
            if judgments_jev_token:
                judgments_values["jev_token"] = judgments_jev_token
            if judgments_jev_enabled is not None:
                judgments_values["jev_decisions_enabled"] = bool(judgments_jev_enabled)
            values["judgments"] = judgments_values
        self._refuse_keyless_routes(provider_values)
        self._write_config(values)

    def save_jev_enabled(self, enabled: bool) -> None:
        """Persist the Jev judgment-model master toggle without touching routing."""
        values = dict(load_config(self.paths, environ={}).values)
        existing_judgments = values.get("judgments", {})
        judgments_values = (
            dict(existing_judgments) if isinstance(existing_judgments, Mapping) else {}
        )
        judgments_values["jev_decisions_enabled"] = bool(enabled)
        values["judgments"] = judgments_values
        self._write_config(values)

    def save_jev_token(self, token: str) -> None:
        """Persist the Jev judgment-model API key without touching routing."""
        token = token.strip()
        if not token:
            raise ValueError("Jev API key is required")
        values = dict(load_config(self.paths, environ={}).values)
        existing_judgments = values.get("judgments", {})
        judgments_values = (
            dict(existing_judgments) if isinstance(existing_judgments, Mapping) else {}
        )
        judgments_values["jev_token"] = token
        values["judgments"] = judgments_values
        self._write_config(values)

    def save_model_endpoint(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        *,
        output_token_parameter: str | None = None,
        reasoning_parameter: str | None = None,
        requires_reasoning_content_for_tool_calls: bool | None = None,
    ) -> None:
        """Persist a per-model provider endpoint override and its optional key."""
        model = model.strip()
        base_url = base_url.strip()
        if not model or not base_url:
            raise ValueError("model and base URL are required")
        if api_key is not None and _is_obvious_credential_placeholder(api_key, base_url):
            raise ValueError("API key is an obvious placeholder; enter the real provider key")
        values = dict(load_config(self.paths, environ={}).values)
        provider = values.get("provider", {})
        provider_values = dict(provider) if isinstance(provider, Mapping) else {}
        endpoints = provider_values.get("model_endpoints", {})
        endpoint_values = dict(endpoints) if isinstance(endpoints, Mapping) else {}
        endpoint: dict[str, object] = {"base_url": base_url}
        previous = endpoint_values.get(model, {})
        if isinstance(previous, Mapping) and previous.get("base_url") == base_url:
            endpoint.update({str(k): v for k, v in previous.items()})
        if api_key is not None and api_key.strip():
            endpoint["api_key"] = api_key.strip()
        if output_token_parameter is not None:
            if output_token_parameter not in {"max_completion_tokens", "max_tokens"}:
                raise ValueError("output token parameter is unsupported")
            endpoint["output_token_parameter"] = output_token_parameter
        if reasoning_parameter is not None:
            if reasoning_parameter not in {"none", "thinking"}:
                raise ValueError("reasoning parameter is unsupported")
            endpoint["reasoning_parameter"] = reasoning_parameter
        if requires_reasoning_content_for_tool_calls is not None:
            endpoint["requires_reasoning_content_for_tool_calls"] = (
                requires_reasoning_content_for_tool_calls
            )
        endpoint_values[model] = endpoint
        provider_values["model_endpoints"] = endpoint_values
        values["provider"] = provider_values
        self._write_config(values)

    def remove_model_endpoint(self, model: str) -> None:
        values = dict(load_config(self.paths, environ={}).values)
        provider = values.get("provider", {})
        provider_values = dict(provider) if isinstance(provider, Mapping) else {}
        endpoints = provider_values.get("model_endpoints", {})
        endpoint_values = dict(endpoints) if isinstance(endpoints, Mapping) else {}
        endpoint_values.pop(model.strip(), None)
        if endpoint_values:
            provider_values["model_endpoints"] = endpoint_values
        else:
            provider_values.pop("model_endpoints", None)
        values["provider"] = provider_values
        self._write_config(values)

    def _refuse_keyless_routes(self, provider_values: Mapping[str, object]) -> None:
        credentials = CredentialStore(self.paths).load(environ={})
        missing = missing_route_credentials(provider_values, credentials)
        if missing:
            raise ValueError(
                "No credential resolves for routed model(s): "
                + ", ".join(missing)
                + "; "
                + _NO_CREDENTIAL_ADVICE
            )

    def _write_config(self, values: Mapping[str, object]) -> None:
        _write_config(self.paths, values)


class BrowserConfiguration:
    """Persist one private browser profile and its exact reviewed origin allowlist."""

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self.paths = paths or ConfigPaths.default()

    def save_profile(self, profile_directory: Path, origins: Sequence[str]) -> None:
        profile = profile_directory.expanduser().resolve()
        if not origins:
            raise ValueError("at least one persistent browser origin is required")
        normalized = []
        for origin in origins:
            parsed = urlsplit(origin.strip())
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("persistent browser entries must be exact HTTP(S) origins")
            normalized.append(f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("persistent browser origins must not contain duplicates")
        values = dict(load_config(self.paths, environ={}).values)
        current = values.get("browser", {})
        browser = dict(current) if isinstance(current, Mapping) else {}
        browser["profile_directory"] = str(profile)
        browser["persistent_origins"] = normalized
        values["browser"] = browser
        _write_config(self.paths, values)
        load_config(self.paths, environ={})


class McpConfiguration:
    """Persist safe MCP launch settings while leaving credentials out of TOML."""

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self.paths = paths or ConfigPaths.default()

    def save_server(
        self, name: str, command: str, args: Sequence[str], cwd: Path | None = None
    ) -> None:
        values = dict(load_config(self.paths, environ={}).values)
        current_mcp = values.get("mcp", {})
        mcp = dict(current_mcp) if isinstance(current_mcp, Mapping) else {}
        current_servers = mcp.get("servers", {})
        servers = dict(current_servers) if isinstance(current_servers, Mapping) else {}
        server: dict[str, object] = {"command": command, "args": list(args)}
        if cwd is not None:
            server["cwd"] = str(cwd)
        servers[name] = server
        mcp["servers"] = servers
        values["mcp"] = mcp
        _write_config(self.paths, values)

    def save_remote_server(
        self,
        name: str,
        url: str,
        credential_handle: str,
        *,
        transport: str,
        disabled: bool = False,
        reconnect_attempts: int = 1,
    ) -> None:
        """Persist only a secret reference and reviewed remote connection policy."""
        values, mcp, servers = self._server_values()
        servers[name] = {
            "remote_url": url,
            "transport": transport,
            "credential_handle": credential_handle,
            "disabled": disabled,
            "reconnect_attempts": reconnect_attempts,
        }
        mcp["servers"] = servers
        values["mcp"] = mcp
        _write_config(self.paths, values)

    def set_disabled(self, name: str, disabled: bool) -> None:
        values, mcp, servers = self._server_values()
        raw = servers.get(name)
        if not isinstance(raw, Mapping):
            raise KeyError(f"unknown MCP server {name!r}")
        servers[name] = dict(raw) | {"disabled": disabled}
        mcp["servers"] = servers
        values["mcp"] = mcp
        _write_config(self.paths, values)

    def remove_server(self, name: str) -> None:
        values, mcp, servers = self._server_values()
        servers.pop(name, None)
        mcp["servers"] = servers
        values["mcp"] = mcp
        _write_config(self.paths, values)

    def _server_values(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        values = dict(load_config(self.paths, environ={}).values)
        current_mcp = values.get("mcp", {})
        mcp = dict(current_mcp) if isinstance(current_mcp, Mapping) else {}
        current_servers = mcp.get("servers", {})
        servers = dict(current_servers) if isinstance(current_servers, Mapping) else {}
        return values, mcp, servers


def _write_config(paths: ConfigPaths, values: Mapping[str, object]) -> None:
    directory = paths.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary:
            tomli_w.dump(dict(values), temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, paths.config)
        temporary_name = ""
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
