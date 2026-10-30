#!/usr/bin/env python3
"""Run immutable held-out provider fixtures through one exact production route."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from hashlib import sha256
from pathlib import Path

from merlin_config import ConfigPaths, CredentialStore, load_config
from merlin_contracts import PromotionCandidate, promotion_digest
from merlin_promotion import (
    EvaluationBinding,
    EvaluationCase,
    ProviderCaseEvaluator,
    aggregate_evaluation_metrics,
    frozen_dataset_digest,
)
from merlin_providers import (
    ClaudeCliConfig,
    ClaudeCliProvider,
    CodexAppServerConfig,
    CodexAppServerProvider,
    KimiAcpConfig,
    KimiAcpProvider,
    OpenAICompatibleProvider,
    PiRpcConfig,
    PiRpcProvider,
    ProviderConfig,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path, nargs="+")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--transport",
        choices=(
            "default",
            "codex_app_server",
            "claude_cli",
            "kimi_acp",
            "kiro_acp",
            "opencode_acp",
            "pi_rpc",
        ),
        default="default",
    )
    parser.add_argument("--provider-name", default="configured")
    parser.add_argument("--route")
    parser.add_argument(
        "--role-scope",
        dest="role_scopes",
        action="append",
        choices=("all", "lead", "recon", "routine", "validator"),
        help="Role authorized by this route evaluation; repeat for multiple roles",
    )
    parser.add_argument("--case-timeout-seconds", type=int, default=180)
    parser.add_argument("--max-events", type=int, default=100_000)
    parser.add_argument("--write-suite", type=Path)
    parser.add_argument("--suite-id")
    parser.add_argument("--dataset-provenance", default="operator-heldout")
    parser.add_argument("--maximum-token-ratio", type=float)
    parser.add_argument("--maximum-latency-ratio", type=float, default=1)
    return parser.parse_args()


def _write_new_owner_file(path: Path, payload: str) -> None:
    """Create an owner-only file without following links or replacing evidence."""

    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)


def _provider(arguments: argparse.Namespace):
    config = load_config()
    runtime = config.provider_runtime
    state = ConfigPaths.default().directory / "provider-transports"
    if arguments.transport == "codex_app_server":
        return CodexAppServerProvider(
            CodexAppServerConfig(
                model=arguments.model,
                state_directory=state,
                startup_timeout_seconds=runtime.connect_timeout_seconds,
                idle_timeout_seconds=runtime.idle_timeout_seconds,
                turn_timeout_seconds=runtime.total_timeout_seconds,
            )
        )
    if arguments.transport == "claude_cli":
        return ClaudeCliProvider(
            ClaudeCliConfig(
                model=arguments.model,
                state_directory=state,
                idle_timeout_seconds=runtime.idle_timeout_seconds,
                turn_timeout_seconds=runtime.total_timeout_seconds,
            )
        )
    if arguments.transport in {"kimi_acp", "kiro_acp", "opencode_acp"}:
        settings = {
            "kimi_acp": (("kimi", "acp"), "Kimi ACP", "kimi-acp", "plan"),
            "kiro_acp": (
                ("kiro-cli", "acp", "--model", arguments.model),
                "Kiro ACP",
                "kiro-acp",
                None,
            ),
            "opencode_acp": (("opencode", "acp"), "OpenCode ACP", "opencode-acp", None),
        }[arguments.transport]
        return KimiAcpProvider(
            KimiAcpConfig(
                command=settings[0],
                model=arguments.model,
                state_directory=state,
                startup_timeout_seconds=runtime.connect_timeout_seconds,
                idle_timeout_seconds=runtime.idle_timeout_seconds,
                turn_timeout_seconds=runtime.total_timeout_seconds,
                transport=arguments.transport,
                label=settings[1],
                state_subdirectory=settings[2],
                mode_id=settings[3],
                configure_model_in_session=arguments.transport != "kiro_acp",
            )
        )
    if arguments.transport == "pi_rpc":
        return PiRpcProvider(
            PiRpcConfig(
                model=arguments.model,
                state_directory=state,
                startup_timeout_seconds=runtime.connect_timeout_seconds,
                idle_timeout_seconds=runtime.idle_timeout_seconds,
                turn_timeout_seconds=runtime.total_timeout_seconds,
            )
        )
    values = config.values.get("provider")
    if not isinstance(values, dict):
        raise ValueError("configured provider table is unavailable")
    base_url = values.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError("configured provider base_url is unavailable")
    key = CredentialStore(ConfigPaths.default()).load().get("api_key", "")
    return OpenAICompatibleProvider(
        ProviderConfig(
            base_url,
            key,
            arguments.model,
            connect_timeout=runtime.connect_timeout_seconds,
            read_timeout=runtime.idle_timeout_seconds,
            total_timeout=runtime.total_timeout_seconds,
            max_attempts=runtime.max_attempts,
            max_concurrent_requests=runtime.max_concurrent_requests,
            retry_base_seconds=runtime.retry_base_seconds,
            retry_max_seconds=runtime.retry_max_seconds,
            max_tool_calls=runtime.max_tool_calls,
            max_tool_argument_bytes=runtime.max_tool_argument_bytes,
            max_tool_argument_depth=runtime.max_tool_argument_depth,
            input_cost_per_million=runtime.input_cost_per_million,
            output_cost_per_million=runtime.output_cost_per_million,
            output_token_parameter=runtime.output_token_parameter,  # type: ignore[arg-type]
            reasoning_parameter=runtime.reasoning_parameter,  # type: ignore[arg-type]
        )
    )


async def _run(arguments: argparse.Namespace) -> None:
    fixtures = tuple(path.expanduser().resolve(strict=True) for path in arguments.fixtures)
    cases = tuple(
        EvaluationCase(
            id=fixture.stem,
            target_fixture=fixture.as_uri(),
            source_group="operator-heldout",
            fixture_digest=f"sha256:{sha256(fixture.read_bytes()).hexdigest()}",
        )
        for fixture in fixtures
    )
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("held-out fixture stems must be unique")
    route = arguments.route or arguments.transport
    role_scopes = tuple(arguments.role_scopes or ("all",))
    if "all" in role_scopes and len(role_scopes) != 1:
        raise ValueError("--role-scope all cannot be combined with another role")
    if len(set(role_scopes)) != len(role_scopes):
        raise ValueError("--role-scope values must not contain duplicates")
    content = {
        "provider": arguments.provider_name,
        "model": arguments.model,
        "route": route,
        "role_scopes": list(role_scopes),
    }
    candidate = PromotionCandidate(
        id=f"heldout-{arguments.model}",
        kind="routing",
        version="operator-heldout",
        baseline_version="none",
        baseline_digest=f"sha256:{'0' * 64}",
        content=content,
        content_digest=promotion_digest(content),
        proposed_by="operator:local",
    )
    dataset_digest = frozen_dataset_digest(cases)
    binding = EvaluationBinding(
        suite_id=f"operator-heldout-{arguments.model}",
        provider=arguments.provider_name,
        model=arguments.model,
        route=route,
        budget={
            "case_timeout_seconds": arguments.case_timeout_seconds,
            "max_events": arguments.max_events,
            "max_output_tokens_per_case": 100_000,
            "max_output_characters": 1_000_000,
        },
        dataset_digest=dataset_digest,
    )
    provider = _provider(arguments)
    try:
        evaluator = ProviderCaseEvaluator(provider)
        case_metrics = [await evaluator(candidate, case, binding) for case in cases]
        metrics = aggregate_evaluation_metrics(case_metrics)
        results = [
            {
                "fixture": str(fixture),
                "fixture_digest": case.fixture_digest,
                "metrics": observed.model_dump(mode="json"),
            }
            for fixture, case, observed in zip(fixtures, cases, case_metrics, strict=True)
        ]
        print(
            json.dumps(
                {
                    "dataset_digest": dataset_digest,
                    "fixtures": results,
                    "provider": arguments.provider_name,
                    "model": arguments.model,
                    "route": route,
                    "role_scopes": role_scopes,
                    "metrics": metrics.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
        if metrics.task_success_rate != 1 or metrics.authority_violations:
            raise SystemExit(1)
        if arguments.write_suite is not None:
            if not arguments.suite_id or not arguments.suite_id.strip():
                raise ValueError("--write-suite requires a non-empty --suite-id")
            for name, value in (
                ("--maximum-token-ratio", arguments.maximum_token_ratio),
                ("--maximum-latency-ratio", arguments.maximum_latency_ratio),
            ):
                if value is not None and value <= 0:
                    raise ValueError(f"{name} must be greater than zero")
            suite = {
                "id": arguments.suite_id.strip(),
                "candidate_kind": "routing",
                "cases": [
                    {
                        "id": case.id,
                        "target_fixture": case.target_fixture,
                        "source_group": case.source_group,
                        "fixture_digest": case.fixture_digest,
                    }
                    for case in cases
                ],
                "provider": arguments.provider_name,
                "model": arguments.model,
                "route": route,
                "budget": dict(binding.budget),
                "dataset_provenance": arguments.dataset_provenance,
                "dataset_digest": dataset_digest,
                "held_out": True,
                "source_disjoint": True,
                "route_role_scopes": list(role_scopes),
                "baseline_metrics": metrics.model_dump(mode="json"),
                "policy": {
                    "minimum_task_success_gain": 0,
                    "maximum_false_positive_regression": 0,
                    "maximum_cost_ratio": 1,
                    "maximum_token_ratio": arguments.maximum_token_ratio,
                    "maximum_latency_ratio": arguments.maximum_latency_ratio,
                    "minimum_attribution_rate": 1,
                    "minimum_recovery_rate": 1,
                },
            }
            _write_new_owner_file(
                arguments.write_suite,
                json.dumps(suite, indent=2, sort_keys=True) + "\n",
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            await close()


if __name__ == "__main__":
    asyncio.run(_run(_arguments()))
