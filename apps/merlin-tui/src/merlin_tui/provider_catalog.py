from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    id: str
    label: str
    base_url: str
    models_url: str
    needs_key: bool
    local: bool = False
    response_style: str = "openai"
    output_token_parameter: str = "max_tokens"
    reasoning_parameter: str = "none"


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    id: str
    vision: bool | None = None
    context_window_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelDiscovery:
    models: tuple[ModelCandidate, ...] = ()
    error: str | None = None


def discover_claude_cli(timeout: float = 10.0) -> ModelDiscovery:
    """Check that Claude CLI is installed and authenticated without exposing account data."""
    executable = shutil.which("claude")
    if executable is None:
        return ModelDiscovery(
            error="Claude CLI was not found in Merlin's PATH; install it and reopen Merlin."
        )
    try:
        status = subprocess.run(
            (executable, "auth", "status"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ModelDiscovery(error="Claude CLI authentication check timed out; retry /model.")
    except OSError:
        return ModelDiscovery(error="Claude CLI could not be launched from Merlin's PATH.")
    if status.returncode != 0:
        return ModelDiscovery(
            error="Claude CLI is not authenticated; run `claude auth login`, then retry /model."
        )
    # Claude CLI does not expose a model catalogue. These documented aliases
    # make the common path selectable while retaining custom model entry.
    return ModelDiscovery((ModelCandidate("sonnet"), ModelCandidate("opus")))


# Routes are stable protocol configuration. Model IDs deliberately do not live
# here: setup discovers the provider's current catalog from the provider itself.
PROVIDER_ROUTES = (
    ProviderRoute(
        "ollama",
        "Ollama — local models on this Mac (no API key)",
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/api/tags",
        False,
        local=True,
        response_style="ollama",
    ),
    ProviderRoute(
        "lm-studio",
        "LM Studio — local models on this Mac (no API key)",
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:1234/v1/models",
        False,
        local=True,
    ),
    ProviderRoute(
        "openai",
        "OpenAI — hosted",
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/models",
        True,
        output_token_parameter="max_completion_tokens",
    ),
    ProviderRoute(
        "deepseek",
        "DeepSeek — hosted",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/models",
        True,
        reasoning_parameter="thinking",
    ),
    ProviderRoute(
        "together",
        "Together AI — hosted",
        "https://api.together.ai/v1",
        "https://api.together.ai/v1/models",
        True,
        response_style="together",
    ),
    ProviderRoute(
        "openrouter",
        "OpenRouter — hosted",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/models",
        True,
    ),
    ProviderRoute(
        "gemini",
        "Google Gemini — OpenAI-compatible API",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://generativelanguage.googleapis.com/v1beta/openai/models",
        True,
    ),
    ProviderRoute(
        "custom",
        "Other OpenAI-compatible provider — any endpoint",
        "",
        "",
        True,
    ),
)


def discover_models(
    route: ProviderRoute,
    api_key: str = "",
    *,
    base_url: str | None = None,
    timeout: float = 3.0,
) -> tuple[str, ...]:
    """Compatibility projection of the provider's live model IDs."""
    return tuple(
        item.id
        for item in discover_model_catalog(
            route, api_key, base_url=base_url, timeout=timeout
        ).models
    )


def discover_model_catalog(
    route: ProviderRoute,
    api_key: str = "",
    *,
    base_url: str | None = None,
    timeout: float = 3.0,
) -> ModelDiscovery:
    """Discover current models and advertised capabilities with actionable failures."""
    models_url = route.models_url
    if route.id == "custom":
        models_url = f"{(base_url or '').rstrip('/')}/models"
    if not models_url:
        return ModelDiscovery(error="This provider does not expose a model-catalog endpoint.")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(models_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload: Any = response.json()
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status in {401, 403}:
            message = "Model discovery was rejected; verify the API key and provider account."
        elif status == 429:
            message = "Model discovery was rate limited; retry later or enter an exact model ID."
        else:
            message = f"Model discovery returned HTTP {status}; verify the provider endpoint."
        return ModelDiscovery(error=message)
    except httpx.HTTPError as error:
        return ModelDiscovery(
            error=f"Could not reach the model catalog at {models_url}: {type(error).__name__}."
        )
    except (ValueError, TypeError):
        return ModelDiscovery(error="The provider returned a malformed model catalog.")
    if route.response_style == "ollama":
        rows = payload.get("models", []) if isinstance(payload, dict) else []
        candidates = [_candidate(row, id_key="name") for row in rows if isinstance(row, dict)]
    elif route.response_style == "together":
        rows = payload if isinstance(payload, list) else []
        candidates = [_candidate(row) for row in rows if isinstance(row, dict)]
    else:
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        candidates = [_candidate(row) for row in rows if isinstance(row, dict)]
    unique = {item.id: item for item in candidates if item is not None}
    if not unique:
        return ModelDiscovery(error="The provider model catalog contained no usable model IDs.")
    return ModelDiscovery(tuple(unique.values()))


async def discover_routing_models(
    provider: Mapping[str, object], api_key: str = ""
) -> Mapping[str, ModelDiscovery]:
    """Discover live catalogues for transports that expose a trustworthy API."""
    base_url = str(provider.get("base_url", ""))
    route = next(
        (candidate for candidate in PROVIDER_ROUTES if candidate.base_url == base_url),
        PROVIDER_ROUTES[-1],
    )
    openai_task = asyncio.to_thread(
        discover_model_catalog,
        route,
        api_key,
        base_url=base_url,
    )
    openai, codex, claude = await asyncio.gather(
        openai_task,
        discover_codex_models(),
        asyncio.to_thread(discover_claude_cli),
    )
    unavailable = ModelDiscovery(
        error="This transport does not expose a reliable live model catalogue."
    )
    return {
        "openai_compatible": openai,
        "codex_app_server": codex,
        "claude_cli": claude,
        "kimi_acp": unavailable,
        "kiro_acp": unavailable,
        "opencode_acp": unavailable,
    }


async def discover_codex_models() -> ModelDiscovery:
    """Read the authenticated Codex App Server model picker via model/list."""
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "-c",
            "mcp_servers={}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None and process.stdout is not None

        async def request(identifier: int, method: str, params: object) -> Mapping[str, Any]:
            process.stdin.write(
                (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            )
            await process.stdin.drain()
            async with asyncio.timeout(10.0):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise RuntimeError("Codex App Server closed during model discovery")
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get("id") == identifier:
                        if "error" in message:
                            error = message.get("error")
                            detail = error.get("message") if isinstance(error, dict) else error
                            raise RuntimeError(str(detail or "Codex model discovery failed"))
                        result = message.get("result", {})
                        return result if isinstance(result, dict) else {}

        await request(
            1,
            "initialize",
            {"clientInfo": {"name": "merlin", "version": "0.1.0"}, "capabilities": {}},
        )
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        await process.stdin.drain()
        result = await request(2, "model/list", {"includeHidden": False, "limit": 100})
        rows = result.get("data", [])
        candidates = tuple(
            ModelCandidate(str(row["model"]))
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("model"), str)
            and str(row["model"]).strip()
            and not row.get("hidden", False)
        )
        if not candidates:
            return ModelDiscovery(error="Codex returned no selectable models.")
        return ModelDiscovery(tuple(dict.fromkeys(candidates)))
    except (OSError, RuntimeError, TimeoutError, ValueError, TypeError) as error:
        return ModelDiscovery(error=f"Could not discover Codex models: {error}")
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2.0)
            except TimeoutError:
                process.kill()
                await process.wait()


def _candidate(row: dict[str, Any], *, id_key: str = "id") -> ModelCandidate | None:
    identifier = row.get(id_key)
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    context = next(
        (
            value
            for key in ("context_length", "context_window", "max_context_length")
            if isinstance((value := row.get(key)), int) and value >= 1_024
        ),
        None,
    )
    architecture = row.get("architecture")
    modalities = row.get("input_modalities")
    if isinstance(architecture, dict):
        modalities = architecture.get("input_modalities", modalities)
    vision = None
    if isinstance(modalities, list):
        vision = any(str(item).casefold() in {"image", "images", "vision"} for item in modalities)
    return ModelCandidate(identifier.strip(), vision=vision, context_window_tokens=context)
