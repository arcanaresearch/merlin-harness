"""Construction of providers for non-secret AI target profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from merlin_contracts import TargetProfile

from .openai_compatible import OpenAICompatibleProvider, ProviderConfig


class TargetCredentialError(ValueError):
    """The credential selected for a target is unavailable.

    A missing credential is an operational input error, so the durable tool
    boundary converts it into an error result for the calling worker instead of
    letting it tear down the wave and fail the whole campaign.
    """


@dataclass(frozen=True, slots=True)
class TargetSessionIdentity:
    target_id: str
    credential_handle: str
    authenticated: bool
    server_identity: str | None
    status_code: int


class TargetProviderFactory:
    """Build target providers while keeping credentials outside target profiles."""

    def __init__(self, credentials: Mapping[str, str]) -> None:
        self._credentials = credentials

    def create(
        self,
        profile: TargetProfile,
        secret_lookup_key: str | None = None,
        *,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> OpenAICompatibleProvider:
        """Resolve a secret at the boundary and build a provider for ``profile``."""
        secret_lookup_key = secret_lookup_key or self.credential_key(profile)
        if not secret_lookup_key.strip():
            raise ValueError("secret_lookup_key must not be empty")
        try:
            api_key = self._credentials[secret_lookup_key]
        except KeyError as exc:
            raise TargetCredentialError(
                f"credential {secret_lookup_key!r} is not configured"
            ) from exc

        return OpenAICompatibleProvider(
            ProviderConfig(
                base_url=profile.base_url,
                api_key=api_key,
                model=profile.model,
                timeout=timeout,
            ),
            client=client,
        )

    async def preflight(
        self,
        profile: TargetProfile,
        secret_lookup_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> TargetSessionIdentity:
        """Verify target reachability/authentication without persisting its secret."""
        key = secret_lookup_key or self.credential_key(profile)
        if not key.strip():
            raise ValueError("secret_lookup_key must not be empty")
        try:
            secret = self._credentials[key]
        except KeyError as exc:
            raise TargetCredentialError(f"credential {key!r} is not configured") from exc
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=timeout)
        try:
            response = await client.get(
                profile.base_url,
                headers={"Authorization": f"Bearer {secret}"},
            )
        finally:
            if owns_client:
                await client.aclose()
        return TargetSessionIdentity(
            target_id=profile.id,
            credential_handle=key,
            authenticated=response.status_code not in {401, 403},
            server_identity=response.headers.get("x-merlin-session-id"),
            status_code=response.status_code,
        )

    @staticmethod
    def credential_key(profile: TargetProfile) -> str:
        """Return the conventional credential key for a target profile."""
        return f"target_{profile.id}"
