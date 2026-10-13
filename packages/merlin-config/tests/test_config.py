import json
from pathlib import Path

import pytest
from merlin_config import ConfigPaths, load_config


def test_loads_toml_and_environment_overrides_database_url(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text('[database]\nurl = "postgresql://file"\n', encoding="utf-8")
    paths.config.chmod(0o600)

    config = load_config(paths, {"MERLIN_DATABASE_URL": "postgresql://environment"})

    assert config.database_url == "postgresql://environment"
    assert config.values["database"] == {"url": "postgresql://environment"}


def test_auto_is_the_default_and_is_valid_when_explicit(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    assert load_config(paths, {}).values.get("permission_mode", "auto") == "auto"
    paths.config.write_text('permission_mode = "auto"\n', encoding="utf-8")
    paths.config.chmod(0o600)
    assert load_config(paths, {}).values["permission_mode"] == "auto"


def test_rejects_config_readable_by_group(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text("", encoding="utf-8")
    paths.config.chmod(0o640)

    with pytest.raises(PermissionError):
        load_config(paths, {})


def test_output_token_parameter_environment_override(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    config = load_config(paths, {"MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER": "max_tokens"})
    assert config.values["provider"]["output_token_parameter"] == "max_tokens"
    assert config.sources["provider.output_token_parameter"] == (
        "environment:MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER"
    )
    assert config.provider_runtime.output_token_parameter == "max_tokens"

    with pytest.raises(ValueError, match="output_token_parameter"):
        load_config(paths, {"MERLIN_PROVIDER_OUTPUT_TOKEN_PARAMETER": "bogus"})


def test_precedence_validation_redaction_and_reload_classification(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        'permission_mode="prompt"\n[provider]\nbase_url="https://file"\nmodel="file"\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    config = load_config(
        paths,
        {"MERLIN_PROVIDER_MODEL": "environment"},
        {"provider.model": "cli", "permission_mode": "yolo"},
    )
    assert config.values["provider"]["model"] == "cli"
    assert config.values["permission_mode"] == "yolo"
    assert config.sources == {
        "permission_mode": "cli",
        "provider.base_url": "file",
        "provider.model": "cli",
    }
    assert config.redacted()["provider"]["model"] == "cli"
    older = load_config(paths, {})
    reloadable, restart = older.changed_settings(config)
    assert reloadable == frozenset({"permission_mode", "provider.model"})
    assert restart == frozenset()

    diagnostics = config.diagnostics()
    assert diagnostics["sources"] == config.sources
    assert diagnostics["effective"]["runtime"]["campaign_max_turns"] is None
    assert diagnostics["effective"]["runtime"]["local_control_max_stream_events"] == 65_536
    assert diagnostics["reload_safe"] == [
        "max_coverage_extensions",
        "permission_mode",
        "provider.context_window_tokens",
        "provider.model",
        "provider.model_capabilities",
        "provider.model_transports",
        "provider.role_models",
        "specialist_composition",
    ]


def test_route_table_changes_are_reloadable_at_the_route_prefix(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        '[provider]\nmodel = "a"\n\n[provider.role_models]\nlead = "lead-old"\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    older = load_config(paths, {})
    paths.config.write_text(
        '[provider]\nmodel = "a"\n\n[provider.role_models]\nlead = "lead-new"\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    reloadable, restart = older.changed_settings(load_config(paths, {}))
    assert reloadable == frozenset({"provider.role_models"})
    assert restart == frozenset()


def test_unknown_and_malformed_settings_fail_early(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text("mystery = true\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="unknown configuration"):
        load_config(paths, {})


def test_evaluation_suite_files_require_unique_absolute_paths(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    suite = tmp_path / "suite.json"
    paths.config.write_text(
        f"[evaluation]\nsuite_files = [{json.dumps(str(suite))}]\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).evaluation_suite_files == (suite,)

    paths.config.write_text(
        f"[evaluation]\nsuite_files = [{json.dumps(str(suite))}, {json.dumps(str(suite))}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_config(paths, {})


def test_persistent_browser_profile_requires_explicit_exact_origins(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    profile = tmp_path / "browser-profile"
    paths.config.write_text(
        "[browser]\n"
        f"profile_directory = {json.dumps(str(profile))}\n"
        'persistent_origins = ["https://target.test"]\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    browser = load_config(paths, {}).values["browser"]
    assert browser["persistent_origins"] == ["https://target.test"]

    paths.config.write_text(
        '[browser]\npersistent_origins = ["https://target.test"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="requires browser.profile_directory"):
        load_config(paths, {})

    paths.config.write_text(
        "[browser]\n"
        f"profile_directory = {json.dumps(str(profile))}\n"
        'persistent_origins = ["https://target.test/chat"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"HTTP\(S\) origins"):
        load_config(paths, {})

    paths.config.write_text(
        "[browser]\n"
        f"profile_directory = {json.dumps(str(profile))}\n"
        'persistent_origins = ["https://target.test", "https://TARGET.test/"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_config(paths, {})

    paths.config.write_text('[evaluation]\nsuite_files = ["relative.json"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="absolute"):
        load_config(paths, {})


def test_redacts_nested_secret_shaped_values() -> None:
    from merlin_config import Config

    config = Config(values={"mcp": {"servers": {"x": {"credential_handle": "vault:one"}}}})
    assert config.redacted()["mcp"]["servers"]["x"]["credential_handle"] == "[REDACTED]"
    assert "vault:one" not in str(config.diagnostics())


def test_runtime_tuning_is_centralized_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[runtime]\nagent_lease_seconds=45.5\nagent_max_steps_per_turn=2000\n"
        "agent_max_result_repairs=3\nagent_browser_observation_soft_limit=40\n"
        "agent_context_compaction_trigger_ratio=0.85\n"
        "agent_context_compaction_target_ratio=0.45\n"
        "campaign_stall_seconds=900.5\n"
        "campaign_max_turns=128\n"
        "campaign_max_tokens=900000\ncampaign_max_seconds=7200\nknowledge_entry_limit=8\n"
        "campaign_preflight_max_tokens=500000\ncampaign_preflight_max_cost_usd=2.5\n"
        "local_control_max_stream_events=131072\n"
        "local_control_stream_journal_batch=64\n"
        "local_control_request_timeout_seconds=45.5\n"
        "operational_log_retention_entries=20000\n"
        "operational_log_retention_days=60\n"
        "config_reload_poll_seconds=0.25\n"
        "tool_read_max_bytes=524288\n"
        "tool_shell_timeout_seconds=120\n"
        "tool_shell_max_output_bytes=1048576\n"
        "tool_shell_termination_grace_seconds=2.5\n"
        "daemon_startup_seconds=300\n"
        "daemon_shutdown_seconds=60\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    runtime = load_config(paths, {}).runtime

    assert runtime.agent_lease_seconds == 45.5
    assert runtime.agent_max_steps_per_turn == 2_000
    assert runtime.agent_max_result_repairs == 3
    assert runtime.agent_browser_observation_soft_limit == 40
    assert runtime.agent_context_compaction_trigger_ratio == 0.85
    assert runtime.agent_context_compaction_target_ratio == 0.45
    assert runtime.campaign_stall_seconds == 900.5
    assert runtime.campaign_max_turns == 128
    assert runtime.campaign_max_tokens == 900_000
    assert runtime.campaign_max_seconds == 7_200
    assert runtime.campaign_preflight_max_tokens == 500_000
    assert runtime.campaign_preflight_max_cost_usd == 2.5
    assert runtime.knowledge_entry_limit == 8
    assert runtime.learning_evidence_validity_seconds == 86_400
    assert runtime.local_control_max_stream_events == 131_072
    assert runtime.local_control_stream_journal_batch == 64
    assert runtime.local_control_request_timeout_seconds == 45.5
    assert runtime.operational_log_retention_entries == 20_000
    assert runtime.operational_log_retention_days == 60
    assert runtime.config_reload_poll_seconds == 0.25
    assert runtime.tool_read_max_bytes == 524_288
    assert runtime.tool_shell_timeout_seconds == 120
    assert runtime.tool_shell_max_output_bytes == 1_048_576
    assert runtime.tool_shell_termination_grace_seconds == 2.5
    assert runtime.daemon_startup_seconds == 300
    assert runtime.daemon_shutdown_seconds == 60


def test_provider_pricing_rates_are_centralized_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider]\nbase_url='https://provider.test/v1'\nmodel='priced'\n"
        "input_cost_per_million=3.25\noutput_cost_per_million=12.5\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    runtime = load_config(paths, {}).provider_runtime

    assert runtime.input_cost_per_million == 3.25
    assert runtime.output_cost_per_million == 12.5


def test_provider_role_models_are_validated_and_preserve_homogeneous_fallback(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider]\nmodel='fallback'\n[provider.role_models]\n"
        "lead='strong'\nrecon='vision'\nroutine='economical'\n"
        "adversarial-researcher='researcher'\n"
        "novelty-critic='critic'\n"
        "experiment-planner='planner'\n"
        "independent-validator='independent'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    routes = load_config(paths, {}).provider_role_models

    assert routes == {
        "lead": "strong",
        "recon": "vision",
        "routine": "economical",
        "adversarial-researcher": "researcher",
        "novelty-critic": "critic",
        "experiment-planner": "planner",
        "independent-validator": "independent",
    }
    with pytest.raises(TypeError, match="non-empty string"):
        paths.config.write_text("[provider.role_models]\nroutine=''\n", encoding="utf-8")
        load_config(paths, {})


def test_provider_model_transports_are_exact_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_transports]\n"
        "'gpt-5.4'='codex_app_server'\n"
        "'claude-opus'='claude_cli'\n"
        "'kimi-k2'='kimi_acp'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).provider_model_transports == {
        "gpt-5.4": "codex_app_server",
        "claude-opus": "claude_cli",
        "kimi-k2": "kimi_acp",
    }

    paths.config.write_text(
        "[provider.model_transports]\n'gpt-5.4'='unbounded_cli'\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must be one of"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider.model_transports]\ngpt-5.4='codex_app_server'\n", encoding="utf-8"
    )
    with pytest.raises(TypeError, match="quote model IDs containing dots"):
        load_config(paths, {})


def test_provider_native_api_transports_parse(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_transports]\n"
        "'gpt-5.4'='openai_responses'\n"
        "'claude-opus'='anthropic_messages'\n"
        "'gemini-2.5-pro'='gemini'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    assert load_config(paths, {}).provider_model_transports == {
        "gpt-5.4": "openai_responses",
        "claude-opus": "anthropic_messages",
        "gemini-2.5-pro": "gemini",
    }


def test_provider_model_endpoints_are_exact_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_endpoints.'claude-haiku-4-5']\n"
        "base_url='https://api.anthropic.com/v1'\n"
        "api_key='sk-ant-test'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).provider_model_endpoints == {
        "claude-haiku-4-5": {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "sk-ant-test",
        }
    }

    # api_key is optional: a route may re-point only the endpoint.
    paths.config.write_text(
        "[provider.model_endpoints.'deepseek-v4-pro']\nbase_url='https://proxy.example.com/v1'\n",
        encoding="utf-8",
    )
    assert load_config(paths, {}).provider_model_endpoints == {
        "deepseek-v4-pro": {"base_url": "https://proxy.example.com/v1"}
    }

    paths.config.write_text(
        "[provider.model_endpoints.'claude-haiku-4-5']\napi_key='sk-ant-test'\n",
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="base_url must be a non-empty string"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider.model_endpoints.'claude-haiku-4-5']\n"
        "base_url='https://api.anthropic.com/v1'\n"
        "api_key=''\n",
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="api_key must be a non-empty string"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider.model_endpoints.'claude-haiku-4-5']\n"
        "base_url='https://api.anthropic.com/v1'\n"
        "transport='anthropic_messages'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown provider.model_endpoints"):
        load_config(paths, {})


def test_provider_model_endpoint_protocol_controls_are_exact_and_backward_compatible(
    tmp_path: Path,
) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_endpoints.'deepseek-chat']\n"
        "base_url='https://api.deepseek.com/v1'\n"
        "output_token_parameter='max_tokens'\n"
        "reasoning_parameter='thinking'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).provider_model_endpoints["deepseek-chat"] == {
        "base_url": "https://api.deepseek.com/v1",
        "output_token_parameter": "max_tokens",
        "reasoning_parameter": "thinking",
    }

    paths.config.write_text(
        "[provider.model_endpoints.'legacy']\nbase_url='https://legacy.test/v1'\n",
        encoding="utf-8",
    )
    assert load_config(paths, {}).provider_model_endpoints["legacy"] == {
        "base_url": "https://legacy.test/v1"
    }


def test_provider_model_pricing_is_complete_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_pricing.'strong-v2']\n"
        "input_cost_per_million=3.0\noutput_cost_per_million=12.0\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    assert load_config(paths, {}).provider_model_pricing == {"strong-v2": (3.0, 12.0)}

    paths.config.write_text(
        "[provider.model_pricing.'strong-v2']\n"
        "input_cost_per_million=3.0\noutput_cost_per_million=12.0\n"
        "cache_read_input_cost_per_million=0.75\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    config = load_config(paths, {})
    assert config.provider_model_pricing == {"strong-v2": (3.0, 12.0)}
    assert config.provider_model_cache_read_pricing == {"strong-v2": 0.75}
    assert load_config(paths, {}).provider_runtime.cache_read_input_cost_per_million is None

    paths.config.write_text(
        "[provider.model_pricing.'strong-v2']\n"
        "input_cost_per_million=3.0\noutput_cost_per_million=12.0\n"
        "cache_read_input_cost_per_million=-0.75\n",
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="cache_read_input_cost_per_million"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider.model_pricing.bad]\ninput_cost_per_million=1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires input and output rates"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider]\nbase_url='https://provider.test/v1'\nmodel='m'\n"
        "cache_read_input_cost_per_million=0.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be configured together"):
        load_config(paths, {})


def test_provider_model_capabilities_are_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.model_capabilities]\nvision=['vision', 'tools']\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    assert load_config(paths, {}).provider_model_capabilities == {"vision": ("vision", "tools")}

    paths.config.write_text(
        "[provider.model_capabilities]\nbad=['vision', 'vision']\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        load_config(paths, {})


def test_provider_output_token_parameter_is_explicit_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider]\noutput_token_parameter='max_tokens'\nreasoning_parameter='thinking'\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).provider_runtime.output_token_parameter == "max_tokens"
    assert load_config(paths, {}).provider_runtime.reasoning_parameter == "thinking"

    paths.config.write_text(
        "[provider]\noutput_token_parameter='provider_guess'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="output_token_parameter"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider]\nreasoning_parameter='guess'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reasoning_parameter"):
        load_config(paths, {})


def test_provider_reasoning_replay_compatibility_is_boolean_and_preserved(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider]\nrequires_reasoning_content_for_tool_calls=false\n"
        "[provider.model_endpoints.deepseek]\n"
        "base_url='https://proxy.example/v1'\n"
        "requires_reasoning_content_for_tool_calls=true\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    config = load_config(paths, {})
    assert config.provider_runtime.requires_reasoning_content_for_tool_calls is False
    assert (
        config.provider_model_endpoints["deepseek"]["requires_reasoning_content_for_tool_calls"]
        is True
    )

    paths.config.write_text(
        "[provider]\nrequires_reasoning_content_for_tool_calls='false'\n",
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="requires_reasoning_content_for_tool_calls"):
        load_config(paths, {})


def test_provider_escalation_models_are_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider.escalation_models]\nroutine=['strong', 'strongest']\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    assert load_config(paths, {}).provider_escalation_models == {"routine": ("strong", "strongest")}

    paths.config.write_text(
        "[provider.escalation_models]\nroutine=['same', 'same']\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        load_config(paths, {})


def test_campaign_budgets_are_unlimited_by_default(tmp_path: Path) -> None:
    config = load_config(ConfigPaths(tmp_path), {})
    runtime = config.runtime

    assert runtime.agent_max_steps_per_turn is None
    assert runtime.agent_max_result_repairs == 2
    assert runtime.agent_browser_observation_soft_limit == 24
    assert runtime.campaign_stall_seconds == 360
    assert runtime.campaign_max_turns is None
    assert runtime.campaign_max_tokens is None
    assert runtime.campaign_max_seconds is None
    assert runtime.campaign_preflight_max_tokens is None
    assert runtime.campaign_preflight_max_cost_usd is None
    assert runtime.local_control_max_stream_events == 65_536
    assert runtime.local_control_stream_journal_batch == 512
    assert runtime.local_control_request_timeout_seconds == 30
    assert runtime.operational_log_retention_entries == 10_000
    assert runtime.operational_log_retention_days == 30
    assert runtime.config_reload_poll_seconds == 1
    assert runtime.tool_read_max_bytes == 262_144
    assert runtime.tool_shell_timeout_seconds == 30
    assert runtime.tool_shell_max_output_bytes == 262_144
    assert runtime.tool_shell_termination_grace_seconds == 1
    assert runtime.daemon_startup_seconds == 180
    assert runtime.daemon_shutdown_seconds == 30
    assert config.provider_runtime.connect_timeout_seconds == 10
    assert config.provider_runtime.idle_timeout_seconds == 60
    assert config.provider_runtime.total_timeout_seconds == 1_800
    assert config.provider_runtime.max_attempts == 3
    assert config.provider_runtime.max_concurrent_requests == 8
    assert config.provider_runtime.retry_base_seconds == 0.25
    assert config.provider_runtime.retry_max_seconds == 10
    assert config.provider_runtime.max_tool_calls == 32
    assert config.provider_runtime.max_tool_argument_bytes == 262_144
    assert config.provider_runtime.max_tool_argument_depth == 32
    assert config.provider_runtime.worker_retry_attempts == 3
    assert config.provider_runtime.worker_retry_backoff_seconds == 1
    assert config.provider_runtime.vision is False
    assert config.provider_runtime.context_window_tokens is None
    assert config.runtime.cleanup_max_attempts == 3
    assert config.runtime.tool_action_timeout_seconds == 120


def test_provider_runtime_tuning_is_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        '[provider]\nbase_url="https://provider.example/v1"\nmodel="model"\n'
        "connect_timeout_seconds=15\nidle_timeout_seconds=90\n"
        "total_timeout_seconds=600\nmax_attempts=4\nmax_concurrent_requests=5\n"
        "retry_base_seconds=0.5\nretry_max_seconds=20\n"
        "max_tool_calls=64\nmax_tool_argument_bytes=524288\n"
        "max_tool_argument_depth=48\n"
        "worker_retry_attempts=2\n"
        "worker_retry_backoff_seconds=1.5\nvision=true\ncontext_window_tokens=131072\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    tuning = load_config(paths, {}).provider_runtime
    assert tuning.vision is True
    assert tuning.context_window_tokens == 131_072
    assert tuning.connect_timeout_seconds == 15
    assert tuning.idle_timeout_seconds == 90
    assert tuning.total_timeout_seconds == 600
    assert tuning.max_attempts == 4
    assert tuning.max_concurrent_requests == 5
    assert tuning.retry_base_seconds == 0.5
    assert tuning.retry_max_seconds == 20
    assert tuning.max_tool_calls == 64
    assert tuning.max_tool_argument_bytes == 524_288
    assert tuning.max_tool_argument_depth == 48
    assert tuning.worker_retry_attempts == 2
    assert tuning.worker_retry_backoff_seconds == 1.5


def test_runtime_tuning_rejects_unsafe_bounds(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text("[runtime]\nknowledge_entry_limit=13\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="knowledge_entry_limit"):
        load_config(paths, {})

    paths.config.write_text("[runtime]\nconfig_reload_poll_seconds=0.01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="config_reload_poll_seconds"):
        load_config(paths, {})

    paths.config.write_text(
        "[runtime]\nagent_context_compaction_trigger_ratio=0.7\n"
        "agent_context_compaction_target_ratio=0.7\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="target_ratio"):
        load_config(paths, {})


def test_provider_retry_and_tool_assembly_limits_are_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[provider]\nbase_url='https://provider.test/v1'\nmodel='model'\n"
        "retry_base_seconds=2\nretry_max_seconds=1\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="retry_max_seconds"):
        load_config(paths, {})

    paths.config.write_text(
        "[provider]\nbase_url='https://provider.test/v1'\nmodel='model'\n"
        "max_tool_argument_bytes=1000\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_tool_argument_bytes"):
        load_config(paths, {})


def test_mcp_runtime_tuning_is_centralized_and_validated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        "[mcp]\nstartup_timeout_seconds=20\ninvocation_timeout_seconds=90\n"
        "output_timeout_seconds=15\nshutdown_timeout_seconds=12\n"
        "connect_timeout_seconds=18\nhealth_timeout_seconds=9\n"
        "reconnect_attempts=2\nreconnect_delay_seconds=0.5\n"
        "max_tools=256\nmax_schema_bytes=512000\nmax_output_bytes=2000000\n",
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    tuning = load_config(paths, {}).mcp_runtime

    assert tuning.startup_timeout_seconds == 20
    assert tuning.invocation_timeout_seconds == 90
    assert tuning.output_timeout_seconds == 15
    assert tuning.shutdown_timeout_seconds == 12
    assert tuning.connect_timeout_seconds == 18
    assert tuning.health_timeout_seconds == 9
    assert tuning.reconnect_attempts == 2
    assert tuning.reconnect_delay_seconds == 0.5
    assert tuning.max_tools == 256
    assert tuning.max_schema_bytes == 512_000
    assert tuning.max_output_bytes == 2_000_000

    paths.config.write_text("[mcp]\nreconnect_attempts=4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reconnect_attempts"):
        load_config(paths, {})


def test_specialist_composition_defaults_to_opinionated(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    config = load_config(paths, {})
    assert config.specialist_composition == "opinionated"
    assert "specialist_composition" not in config.values
    assert config.diagnostics()["effective"]["specialist_composition"] == "opinionated"


@pytest.mark.parametrize("policy", ["opinionated", "autonomous"])
def test_specialist_composition_accepts_both_policies(tmp_path: Path, policy: str) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(f'specialist_composition = "{policy}"\n', encoding="utf-8")
    paths.config.chmod(0o600)
    config = load_config(paths, {})
    assert config.specialist_composition == policy
    assert config.sources["specialist_composition"] == "file"


def test_specialist_composition_rejects_invalid_values(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    for invalid in ("guided", "OPINIONATED", ""):
        paths.config.write_text(f'specialist_composition = "{invalid}"\n', encoding="utf-8")
        paths.config.chmod(0o600)
        with pytest.raises(ValueError, match="specialist_composition"):
            load_config(paths, {})
    paths.config.write_text("specialist_composition = 42\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="specialist_composition"):
        load_config(paths, {})


def test_specialist_composition_is_independent_and_reload_safe(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    older = load_config(paths, {})
    paths.config.write_text('specialist_composition = "autonomous"\n', encoding="utf-8")
    paths.config.chmod(0o600)
    newer = load_config(paths, {})
    reloadable, restart = older.changed_settings(newer)
    assert reloadable == frozenset({"specialist_composition"})
    assert restart == frozenset()


def test_max_coverage_extensions_defaults_to_none(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    config = load_config(paths, {})
    assert config.max_coverage_extensions is None
    assert "max_coverage_extensions" not in config.values
    assert config.diagnostics()["effective"]["max_coverage_extensions"] is None


def test_max_coverage_extensions_accepts_positive_integer(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text("max_coverage_extensions = 8\n", encoding="utf-8")
    paths.config.chmod(0o600)
    config = load_config(paths, {})
    assert config.max_coverage_extensions == 8
    assert config.sources["max_coverage_extensions"] == "file"


def test_max_coverage_extensions_env_override(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    config = load_config(paths, {"MERLIN_MAX_COVERAGE_EXTENSIONS": "5"})
    assert config.max_coverage_extensions == 5
    assert config.sources["max_coverage_extensions"] == "environment:MERLIN_MAX_COVERAGE_EXTENSIONS"


def test_max_coverage_extensions_rejects_invalid_values(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    for invalid in ("0", "-1", '"many"', "true", "1.5"):
        paths.config.write_text(f"max_coverage_extensions = {invalid}\n", encoding="utf-8")
        paths.config.chmod(0o600)
        with pytest.raises(ValueError, match="max_coverage_extensions"):
            load_config(paths, {})


def test_max_coverage_extensions_is_reload_safe(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    older = load_config(paths, {})
    paths.config.write_text("max_coverage_extensions = 8\n", encoding="utf-8")
    paths.config.chmod(0o600)
    newer = load_config(paths, {})
    reloadable, restart = older.changed_settings(newer)
    assert reloadable == frozenset({"max_coverage_extensions"})
    assert restart == frozenset()


def test_telemetry_defaults_to_enabled(tmp_path: Path) -> None:
    config = load_config(ConfigPaths(tmp_path), {})
    assert config.telemetry.enabled is True
    assert config.telemetry.endpoint == "https://telemetry.plunder.me/v1/logs"
    assert config.telemetry.token is None
    assert config.telemetry.service_name == "merlin-harness"
    assert config.telemetry.client is None


def test_judgments_defaults_and_toml(tmp_path: Path) -> None:
    config = load_config(ConfigPaths(tmp_path), {})
    assert config.judgments.laya_url is None
    assert config.judgments.laya_token is None
    assert config.judgments.jev_url == "https://api.typesafe.ai/v1/systemone"
    assert config.judgments.jev_token is None
    assert config.judgments.jev_decisions_enabled is True
    assert config.judgments.timeout_seconds == 30.0

    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        '[judgments]\nlaya_url = "http://laya.example:8080"\n'
        'laya_token = "secret-token"\njev_url = "https://jev.example/v1/systemone"\n'
        'jev_token = "jev-secret"\ntimeout_seconds = 45\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)
    config = load_config(paths, {})
    assert config.judgments.laya_url == "http://laya.example:8080"
    assert config.judgments.laya_token == "secret-token"
    assert config.judgments.jev_url == "https://jev.example/v1/systemone"
    assert config.judgments.jev_token == "jev-secret"
    assert config.judgments.timeout_seconds == 45.0


def test_judgments_environment_overrides(tmp_path: Path) -> None:
    config = load_config(
        ConfigPaths(tmp_path),
        {
            "MERLIN_JUDGMENTS_JEV_TOKEN": "env-jev-token",
            "MERLIN_JUDGMENTS_JEV_URL": "https://gateway.example/v1/systemone",
        },
    )
    assert config.judgments.jev_token == "env-jev-token"
    assert config.judgments.jev_url == "https://gateway.example/v1/systemone"
    assert config.redacted()["judgments"]["jev_token"] == "[REDACTED]"


def test_judgments_jev_decisions_toggle(tmp_path: Path) -> None:
    config = load_config(
        ConfigPaths(tmp_path),
        {"MERLIN_JUDGMENTS_JEV_DECISIONS": "false"},
    )
    assert config.judgments.jev_decisions_enabled is False

    config = load_config(
        ConfigPaths(tmp_path),
        {"MERLIN_JUDGMENTS_JEV_DECISIONS": "true"},
    )
    assert config.judgments.jev_decisions_enabled is True

    paths = ConfigPaths(tmp_path)
    paths.config.write_text("[judgments]\njev_decisions_enabled = false\n", encoding="utf-8")
    paths.config.chmod(0o600)
    assert load_config(paths, {}).judgments.jev_decisions_enabled is False


def test_telemetry_loads_from_toml(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text(
        '[telemetry]\nenabled = true\nendpoint = "https://otel.example/v1/logs"\n'
        'token = "secret-token"\nservice_name = "merlin-test"\nclient = "acme-l1"\n',
        encoding="utf-8",
    )
    paths.config.chmod(0o600)

    config = load_config(paths, {})

    assert config.telemetry.enabled is True
    assert config.telemetry.endpoint == "https://otel.example/v1/logs"
    assert config.telemetry.token == "secret-token"
    assert config.telemetry.service_name == "merlin-test"
    assert config.telemetry.client == "acme-l1"
    assert config.redacted()["telemetry"]["token"] == "[REDACTED]"


def test_telemetry_environment_overrides(tmp_path: Path) -> None:
    config = load_config(
        ConfigPaths(tmp_path),
        {
            "MERLIN_TELEMETRY_ENABLED": "true",
            "MERLIN_TELEMETRY_ENDPOINT": "https://otel.example/v1/logs",
            "MERLIN_TELEMETRY_TOKEN": "env-token",
            "MERLIN_TELEMETRY_SERVICE_NAME": "env-service",
            "MERLIN_TELEMETRY_CLIENT": "env-client",
        },
    )
    assert config.telemetry.enabled is True
    assert config.telemetry.endpoint == "https://otel.example/v1/logs"
    assert config.telemetry.token == "env-token"
    assert config.telemetry.service_name == "env-service"
    assert config.telemetry.client == "env-client"
    assert config.sources["telemetry.enabled"] == "environment:MERLIN_TELEMETRY_ENABLED"
    assert config.sources["telemetry.client"] == "environment:MERLIN_TELEMETRY_CLIENT"


def test_telemetry_rejects_unknown_settings_and_bad_types(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path)
    paths.config.write_text("[telemetry]\nextra = 1\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(ValueError, match="unknown telemetry setting"):
        load_config(paths, {})

    paths.config.write_text('[telemetry]\nenabled = "sometimes"\n', encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(TypeError, match="telemetry.enabled"):
        load_config(paths, {})

    paths.config.write_text("[telemetry]\nclient = 42\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(TypeError, match="telemetry.client"):
        load_config(paths, {})

    paths.config.write_text("telemetry = 1\n", encoding="utf-8")
    paths.config.chmod(0o600)
    with pytest.raises(TypeError, match="telemetry must be a table"):
        load_config(paths, {})


def test_default_paths_fall_back_to_legacy_dir_only_for_legacy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    assert ConfigPaths.default().directory == home / ".merlin"

    # Runtime scaffolding under ~/.merlin must not mask an existing legacy config.
    (home / ".merlin" / "logs").mkdir(parents=True)
    legacy = home / ".redpen-python"
    legacy.mkdir()
    (legacy / "config.toml").write_text('permission_mode = "prompt"\n', encoding="utf-8")
    assert ConfigPaths.default().directory == legacy

    # A config in ~/.merlin wins once present.
    (home / ".merlin" / "config.toml").write_text("", encoding="utf-8")
    assert ConfigPaths.default().directory == home / ".merlin"
