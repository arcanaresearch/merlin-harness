from __future__ import annotations

import asyncio

import httpx
import pytest
from merlin_contracts import ChatRequest, TargetProfile

from merlin_providers import (
    ProviderConfig,
    TargetCredentialError,
    TargetProviderFactory,
)


def _profile() -> TargetProfile:
    return TargetProfile(
        id="local-target",
        title="Local target",
        base_url="https://target.example/v1",
        model="target-model",
    )


def test_builds_provider_from_profile_and_separately_resolved_secret() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="data: [DONE]\n")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TargetProviderFactory({"target.local": "target-secret"}).create(
                _profile(), "target.local", client=client
            )
            _ = [
                event
                async for event in provider.stream(ChatRequest(model="target-model", messages=[]))
            ]

            assert provider.config.model == "target-model"
            assert provider.config.base_url == "https://target.example/v1"
            assert "target-secret" not in repr(provider.config)

    asyncio.run(run())

    assert requests[0].headers["Authorization"] == "Bearer target-secret"
    assert str(requests[0].url) == "https://target.example/v1/chat/completions"


def test_profile_and_factory_representations_do_not_reveal_secret() -> None:
    secret = "sk-target-never-store"
    profile = _profile()
    factory = TargetProviderFactory({"target.local": secret})
    provider = factory.create(profile, "target.local")

    assert secret not in repr(profile)
    assert secret not in profile.model_dump_json()
    assert secret not in repr(factory)
    assert secret not in repr(provider)
    assert secret not in repr(provider.config)


def test_missing_target_credential_has_actionable_error() -> None:
    with pytest.raises(TargetCredentialError, match="target.local.*not configured"):
        TargetProviderFactory({}).create(_profile(), "target.local")


def test_missing_target_credential_is_an_operational_tool_error() -> None:
    # The durable tool boundary converts ValueError into an error ToolResult for
    # the calling worker. A missing credential in a continuation wave must
    # surface that way, never as an uncaught error that fails the campaign.
    assert issubclass(TargetCredentialError, ValueError)
    with pytest.raises(ValueError, match="not configured"):
        TargetProviderFactory({}).create(_profile())


def test_uses_profile_based_credential_key_by_convention() -> None:
    provider = TargetProviderFactory({"target_local-target": "target-secret"}).create(_profile())

    assert provider.config.api_key == "target-secret"
    assert TargetProviderFactory.credential_key(_profile()) == "target_local-target"


def test_rejects_empty_secret_lookup_key() -> None:
    with pytest.raises(ValueError, match="secret_lookup_key"):
        TargetProviderFactory({}).create(_profile(), "  ")


def test_attacker_provider_config_remains_independent() -> None:
    attacker = ProviderConfig("https://attacker.example/v1", "attacker-key", "attacker")
    target = TargetProviderFactory({"target.key": "target-key"}).create(_profile(), "target.key")

    assert attacker.base_url != target.config.base_url
    assert attacker.model != target.config.model
    assert attacker.api_key != target.config.api_key


def test_target_preflight_returns_non_secret_session_identity() -> None:
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"x-merlin-session-id": "tenant-user-7"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            identity = await TargetProviderFactory(
                {"target_local-target": "target-secret"}
            ).preflight(_profile(), client=client)
        assert identity.authenticated
        assert identity.server_identity == "tenant-user-7"
        assert identity.credential_handle == "target_local-target"
        assert "target-secret" not in repr(identity)

    asyncio.run(run())
    assert seen[0].headers["Authorization"] == "Bearer target-secret"


def test_target_preflight_exposes_authentication_failure_without_secret() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            identity = await TargetProviderFactory(
                {"target_local-target": "target-secret"}
            ).preflight(_profile(), client=client)
        assert not identity.authenticated
        assert identity.status_code == 401

    asyncio.run(run())
