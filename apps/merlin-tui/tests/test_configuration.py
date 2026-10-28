from pathlib import Path

import pytest
from merlin_config import ConfigPaths, CredentialStore, load_config
from merlin_tui.configuration import (
    BrowserConfiguration,
    McpConfiguration,
    ProviderConfiguration,
    missing_route_credentials,
)


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_provider_configuration_uses_owner_only_files(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")

    ProviderConfiguration(paths).save(
        "https://example.test/v1",
        "model-a",
        "secret",
        vision=True,
        context_window_tokens=131_072,
        output_token_parameter="max_tokens",
        reasoning_parameter="thinking",
    )

    assert load_config(paths, environ={}).values["provider"] == {
        "base_url": "https://example.test/v1",
        "model": "model-a",
        "model_capabilities": {"model-a": ["vision"]},
        "context_window_tokens": 131_072,
        "output_token_parameter": "max_tokens",
        "reasoning_parameter": "thinking",
    }
    assert CredentialStore(paths).load(environ={})["api_key"] == "secret"
    assert mode(paths.directory) == 0o700
    assert mode(paths.config) == 0o600
    assert mode(paths.credentials) == 0o600


def test_provider_configuration_rejects_obvious_remote_placeholder_key(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")

    with pytest.raises(ValueError, match="obvious placeholder"):
        ProviderConfiguration(paths).save(
            "https://api.deepseek.com/v1", "deepseek-chat", "secret"
        )

    assert not paths.config.exists()
    assert not paths.credentials.exists()


def test_missing_route_credentials_treats_remote_placeholder_as_missing() -> None:
    provider = {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    }

    assert missing_route_credentials(provider, {"api_key": "secret"}) == ["deepseek-chat"]


def test_provider_effort_is_persisted_by_setup_and_model_routing(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret", effort="high")
    assert load_config(paths, environ={}).values["provider"]["effort"] == "high"

    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        role_efforts={"workers": "low", "lead": "xhigh"},
    )
    provider = load_config(paths, environ={}).values["provider"]
    # Routing no longer rewrites the global default; it stores per-model efforts.
    assert provider["effort"] == "high"
    assert provider["model_efforts"] == {"model-a": "low"}


def test_model_change_resets_unverified_vision_capability(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save(
        "https://example.test/v1",
        "vision-a",
        vision=True,
        context_window_tokens=131_072,
    )

    configuration.save_model("vision-b")

    assert load_config(paths, environ={}).values["provider"] == {
        "base_url": "https://example.test/v1",
        "model": "vision-b",
        "model_capabilities": {"vision-a": ["vision"]},
    }


def test_model_routing_persists_exact_discovered_vision_without_cross_model_leakage(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "text-worker", "secret")

    configuration.save_model_routing(
        "text-worker",
        "openai_compatible",
        heterogeneous=True,
        lead_model="text-lead",
        recon_model="vision-recon",
        recon_transport="openai_compatible",
        model_vision={
            "text-worker": False,
            "text-lead": None,
            "vision-recon": True,
        },
    )

    reloaded = load_config(paths, environ={})
    provider = reloaded.values["provider"]
    assert provider["model_capabilities"] == {
        "text-worker": [],
        "vision-recon": ["vision"],
    }
    assert reloaded.provider_model_capabilities == {
        "text-worker": (),
        "vision-recon": ("vision",),
    }
    assert "vision" not in provider


def test_provider_configuration_round_trips_exact_vision_denial_per_model(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "denied-model", "secret", vision=False)
    configuration.save("https://example.test/v1", "allowed-model", vision=True)

    reloaded = load_config(paths, environ={})

    assert reloaded.values["provider"]["model_capabilities"] == {
        "allowed-model": ["vision"],
        "denied-model": [],
    }
    assert reloaded.provider_model_capabilities == {
        "allowed-model": ("vision",),
        "denied-model": (),
    }


def test_discovery_and_legacy_migration_preserve_exact_vision_denial(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "denied-model", "secret", vision=False)
    configuration.save_model_routing(
        "denied-model",
        "openai_compatible",
        heterogeneous=False,
        model_vision={"denied-model": True},
    )
    assert load_config(paths, environ={}).provider_model_capabilities == {"denied-model": ()}

    legacy_paths = ConfigPaths(tmp_path / "legacy")
    legacy_paths.directory.mkdir(parents=True)
    legacy_paths.directory.chmod(0o700)
    legacy_paths.config.write_text(
        "[provider]\nbase_url='https://example.test/v1'\nmodel='legacy-model'\nvision=false\n",
        encoding="utf-8",
    )
    legacy_paths.config.chmod(0o600)
    ProviderConfiguration(legacy_paths).save_model("new-model")

    legacy = load_config(legacy_paths, environ={})
    assert legacy.values["provider"]["model_capabilities"] == {"legacy-model": []}
    assert "vision" not in legacy.values["provider"]


def test_model_routing_preserves_explicit_capabilities_and_unknown_stays_unknown(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker", "secret")
    values = load_config(paths, environ={}).values
    values["provider"]["model_capabilities"] = {
        "vision-recon": ["tools", "vision"],
        "worker": ["reasoning"],
    }
    configuration._write_config(values)

    configuration.save_model_routing(
        "worker",
        "openai_compatible",
        heterogeneous=False,
        recon_model="vision-recon",
        recon_transport="openai_compatible",
        model_vision={"worker": None, "vision-recon": False},
    )

    capabilities = load_config(paths, environ={}).provider_model_capabilities
    assert capabilities == {
        "vision-recon": ("tools", "vision"),
        "worker": ("reasoning",),
    }


def test_model_route_persists_safe_lead_transport_and_rejects_cli_default(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker-model", "secret")

    configuration.save_model_route("lead-model", "codex_app_server", lead_only=True)

    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model"] == "worker-model"
    assert provider["role_models"] == {"lead": "lead-model", "recon": "lead-model"}
    assert provider["model_transports"] == {"lead-model": "codex_app_server"}
    with pytest.raises(ValueError, match="only be assigned to Campaign Lead"):
        configuration.save_model_route("unsafe", "claude_cli", lead_only=False)


def test_model_route_allows_codex_worker_transport(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker-model", "secret")

    configuration.save_model_route("codex-worker", "codex_app_server", lead_only=False)

    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model"] == "codex-worker"
    assert provider["model_transports"] == {"codex-worker": "codex_app_server"}


def test_model_route_keeps_distinct_openai_vision_role_explicit(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save(
        "https://example.test/v1", "vision-role", "secret", vision=True
    )
    configuration.save_model("text-worker")

    configuration.save_model_route("vision-role", "openai_compatible", lead_only=True)

    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model"] == "text-worker"
    assert provider["role_models"] == {"lead": "vision-role", "recon": "vision-role"}
    assert provider["model_transports"] == {"vision-role": "openai_compatible"}
    assert provider["model_capabilities"] == {"vision-role": ["vision"]}


def test_model_routing_is_atomic_and_homogeneous_mode_removes_lead_override(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "old-worker", "secret")
    configuration.save_model_endpoint(
        "validator-model", "https://anthropic.example.test", "validator-secret"
    )
    configuration.save_model_endpoint(
        "explorer-model", "https://gemini.example.test", "explorer-secret"
    )

    configuration.save_model_routing(
        "worker-model",
        "codex_app_server",
        heterogeneous=True,
        lead_model="lead-model",
        lead_transport="claude_cli",
        recon_model="recon-model",
        recon_transport="openai_compatible",
        validator_model="validator-model",
        validator_transport="anthropic_messages",
        adversarial_explorer_model="explorer-model",
        adversarial_explorer_transport="gemini",
    )
    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model"] == "worker-model"
    assert provider["role_models"] == {
        "lead": "lead-model",
        "recon": "recon-model",
        "validator": "validator-model",
        "adversarial-explorer": "explorer-model",
        "adversarial-researcher": "explorer-model",
        "novelty-critic": "explorer-model",
        "experiment-planner": "explorer-model",
    }
    assert provider["model_transports"] == {
        "worker-model": "codex_app_server",
        "lead-model": "claude_cli",
        "recon-model": "openai_compatible",
        "validator-model": "anthropic_messages",
        "explorer-model": "gemini",
    }

    configuration.save_model_routing(
        "one-model",
        "openai_compatible",
        heterogeneous=False,
    )
    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model"] == "one-model"
    assert "role_models" not in provider
    assert provider["model_transports"] == {"one-model": "openai_compatible"}


def test_model_endpoint_persists_per_model_override_and_removes_it(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker-model", "secret")

    configuration.save_model_endpoint("lead-model", "https://anthropic.test", "endpoint-secret")
    configuration.save_model_endpoint("local-model", "http://127.0.0.1:11434/v1")

    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model_endpoints"] == {
        "lead-model": {"base_url": "https://anthropic.test", "api_key": "endpoint-secret"},
        "local-model": {"base_url": "http://127.0.0.1:11434/v1"},
    }
    configuration.save_model_endpoint("lead-model", "https://anthropic-2.test", "rotated")
    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model_endpoints"]["lead-model"] == {
        "base_url": "https://anthropic-2.test",
        "api_key": "rotated",
    }

    configuration.remove_model_endpoint("lead-model")
    configuration.remove_model_endpoint("local-model")
    provider = load_config(paths, environ={}).values["provider"]
    assert "model_endpoints" not in provider
    with pytest.raises(ValueError, match="model and base URL are required"):
        configuration.save_model_endpoint("", "https://example.test")


def test_model_endpoint_persists_deepseek_native_protocol_controls(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker-model", "secret")

    configuration.save_model_endpoint(
        "deepseek-chat",
        "https://api.deepseek.com/v1",
        "deepseek-secret",
        output_token_parameter="max_tokens",
        reasoning_parameter="thinking",
        requires_reasoning_content_for_tool_calls=False,
    )

    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model_endpoints"]["deepseek-chat"] == {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "deepseek-secret",
        "output_token_parameter": "max_tokens",
        "reasoning_parameter": "thinking",
        "requires_reasoning_content_for_tool_calls": False,
    }

    configuration.save_model_endpoint(
        "deepseek-chat",
        "https://api.deepseek.com/v1",
        "rotated-secret",
    )
    provider = load_config(paths, environ={}).values["provider"]
    assert (
        provider["model_endpoints"]["deepseek-chat"]["requires_reasoning_content_for_tool_calls"]
        is False
    )

    configuration.save_model_endpoint(
        "gpt-compatible",
        "https://gpt-compatible.test/v1",
        reasoning_parameter="reasoning_effort",
    )
    provider = load_config(paths, environ={}).values["provider"]
    assert provider["model_endpoints"]["gpt-compatible"] == {
        "base_url": "https://gpt-compatible.test/v1",
        "reasoning_parameter": "reasoning_effort",
    }


def test_missing_route_credentials_flags_only_unresolvable_routes() -> None:
    credentials = {"api_key": "secret"}
    provider = {
        "base_url": "https://example.test/v1",
        "model": "worker-model",
        "role_models": {"lead": "cli-lead"},
        "model_transports": {
            "cli-lead": "claude_cli",
            "endpoint-lead": "anthropic_messages",
            "local-model": "openai_responses",
            "keyless-native": "openai_responses",
            "keyless-endpoint": "anthropic_messages",
        },
        "model_endpoints": {
            "endpoint-lead": {"base_url": "https://anthropic.test", "api_key": "key"},
            "local-model": {"base_url": "http://127.0.0.1:11434/v1"},
            "keyless-endpoint": {"base_url": "https://anthropic.test"},
        },
    }

    assert missing_route_credentials(provider, credentials) == [
        "keyless-endpoint",
        "keyless-native",
    ]


def test_missing_route_credentials_requires_primary_key_for_remote_openai_compatible() -> None:
    provider = {"base_url": "https://example.test/v1", "model": "worker-model"}

    assert missing_route_credentials(provider, {}) == ["worker-model"]
    assert missing_route_credentials(provider, {"api_key": "secret"}) == []
    local = {"base_url": "http://127.0.0.1:11434/v1", "model": "worker-model"}
    assert missing_route_credentials(local, {}) == []


def test_save_model_routing_refuses_keyless_routes_without_partial_save(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "worker-model", "secret")

    with pytest.raises(ValueError, match="lead-model.*(/setup endpoint)"):
        configuration.save_model_routing(
            "worker-model",
            "openai_compatible",
            heterogeneous=True,
            lead_model="lead-model",
            lead_transport="anthropic_messages",
        )

    provider = load_config(paths, environ={}).values["provider"]
    assert "role_models" not in provider
    assert "model_transports" not in provider

    configuration.save_model_endpoint("lead-model", "https://anthropic.test", "endpoint-secret")
    configuration.save_model_routing(
        "worker-model",
        "openai_compatible",
        heterogeneous=True,
        lead_model="lead-model",
        lead_transport="anthropic_messages",
    )
    provider = load_config(paths, environ={}).values["provider"]
    assert provider["role_models"] == {"lead": "lead-model"}
    assert provider["model_transports"] == {
        "worker-model": "openai_compatible",
        "lead-model": "anthropic_messages",
    }


def test_setup_unknown_context_window_clears_prior_model_capability(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", context_window_tokens=131_072)

    configuration.save("https://example.test/v1", "model-b", context_window_tokens=None)

    assert "context_window_tokens" not in load_config(paths, environ={}).values["provider"]


def test_browser_configuration_persists_private_profile_and_exact_origins(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    profile = tmp_path / "profile"
    configuration = BrowserConfiguration(paths)

    configuration.save_profile(profile, ("https://TARGET.test/",))

    assert load_config(paths, environ={}).values["browser"] == {
        "profile_directory": str(profile),
        "persistent_origins": ["https://target.test"],
    }
    assert mode(paths.config) == 0o600
    with pytest.raises(ValueError, match=r"exact HTTP\(S\) origins"):
        configuration.save_profile(profile, ("https://target.test/chat",))


def test_mcp_configuration_persists_safe_fields_and_preserves_other_values(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    ProviderConfiguration(paths).save("https://example.test/v1", "model-a")
    configuration = McpConfiguration(paths)

    configuration.save_server("local", "python", ("server.py", "--verbose"), tmp_path / "workspace")

    values = load_config(paths, environ={}).values
    assert values["provider"] == {
        "base_url": "https://example.test/v1",
        "model": "model-a",
    }
    assert values["mcp"] == {
        "servers": {
            "local": {
                "command": "python",
                "args": ["server.py", "--verbose"],
                "cwd": str(tmp_path / "workspace"),
            }
        }
    }

    configuration.remove_server("local")
    values = load_config(paths, environ={}).values
    assert values["mcp"] == {"servers": {}}
    assert "provider" in values


def test_remote_mcp_configuration_persists_handle_but_never_credential(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    CredentialStore(paths).save({"remote-prod": "actual-top-secret"})
    configuration = McpConfiguration(paths)

    configuration.save_remote_server(
        "remote", "https://mcp.example.test/mcp", "remote-prod", transport="streamable_http"
    )

    text = paths.config.read_text(encoding="utf-8")
    assert "actual-top-secret" not in text
    assert load_config(paths, environ={}).values["mcp"]["servers"]["remote"] == {
        "remote_url": "https://mcp.example.test/mcp",
        "transport": "streamable_http",
        "credential_handle": "remote-prod",
        "disabled": False,
        "reconnect_attempts": 1,
    }
    configuration.set_disabled("remote", True)
    assert load_config(paths, environ={}).values["mcp"]["servers"]["remote"]["disabled"] is True


def test_telemetry_client_identity_defaults_to_hostname_plus_install_id(tmp_path: Path) -> None:
    from merlin_tui.bootstrap import _telemetry_client_identity

    first = _telemetry_client_identity(None, tmp_path)
    second = _telemetry_client_identity(None, tmp_path)
    assert first == second  # install id persists across restarts
    hostname, _, install_id = first.rpartition("-")
    assert hostname and install_id and len(install_id) == 8
    assert (tmp_path / "telemetry-install-id").read_text().strip() == install_id
    assert _telemetry_client_identity("configured-name", tmp_path) == "configured-name"


def test_model_routing_persists_judgments_endpoint(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret")

    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_url="https://laya.example.test",
        judgments_token="laya-key",
    )
    assert load_config(paths, environ={}).values["judgments"] == {
        "laya_url": "https://laya.example.test",
        "laya_token": "laya-key",
    }

    # Blank token keeps the stored one; URL may be updated on its own.
    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_url="https://laya2.example.test",
    )
    assert load_config(paths, environ={}).values["judgments"] == {
        "laya_url": "https://laya2.example.test",
        "laya_token": "laya-key",
    }


def test_model_routing_leaves_judgments_untouched_when_blank(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret")

    configuration.save_model_routing("model-a", "openai_compatible", heterogeneous=False)
    assert "judgments" not in load_config(paths, environ={}).values


def test_model_routing_persists_jev_token(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret")

    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_jev_token="jev-key",
    )
    assert load_config(paths, environ={}).values["judgments"] == {"jev_token": "jev-key"}

    # Blank Jev key keeps the stored one.
    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_url="https://laya.example.test",
    )
    assert load_config(paths, environ={}).values["judgments"] == {
        "laya_url": "https://laya.example.test",
        "jev_token": "jev-key",
    }


def test_model_routing_persists_jev_decisions_toggle(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret")

    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_jev_enabled=False,
    )
    assert load_config(paths, environ={}).values["judgments"] == {"jev_decisions_enabled": False}
    assert load_config(paths, environ={}).judgments.jev_decisions_enabled is False

    # None leaves the section untouched.
    configuration.save_model_routing("model-a", "openai_compatible", heterogeneous=False)
    assert load_config(paths, environ={}).values["judgments"] == {"jev_decisions_enabled": False}


def test_save_jev_token_preserves_other_judgments_keys(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save("https://example.test/v1", "model-a", "secret")
    configuration.save_model_routing(
        "model-a",
        "openai_compatible",
        heterogeneous=False,
        judgments_jev_enabled=False,
    )

    configuration.save_jev_token("jev-key")
    assert load_config(paths, environ={}).values["judgments"] == {
        "jev_decisions_enabled": False,
        "jev_token": "jev-key",
    }

    with pytest.raises(ValueError, match="Jev API key is required"):
        configuration.save_jev_token("   ")


def test_endpoint_edit_preserves_key_only_for_same_destination(tmp_path):
    paths = ConfigPaths(tmp_path / "merlin")
    configuration = ProviderConfiguration(paths)
    configuration.save_model_endpoint("one", "https://one.test/v1", "key-one")
    configuration.save_model_endpoint("two", "https://two.test/v1", "key-two")
    configuration.save_model_endpoint("one", "https://one.test/v1")
    routes = load_config(paths, environ={}).provider_model_endpoints
    assert routes["one"]["api_key"] == "key-one"
    assert routes["two"]["api_key"] == "key-two"
    configuration.save_model_endpoint("one", "https://new.test/v1")
    assert "api_key" not in load_config(paths, environ={}).provider_model_endpoints["one"]
