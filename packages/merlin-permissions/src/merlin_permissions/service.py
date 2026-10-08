from __future__ import annotations

from collections.abc import Mapping

from .engine import PermissionEngine, PermissionResolver, PermissionStore
from .models import (
    PermissionAudit,
    PermissionEffect,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
    PermissionVerdict,
)


class PermissionService:
    """Kernel-facing policy control and provider-network authorization."""

    def __init__(
        self,
        engine: PermissionEngine,
        store: PermissionStore,
        *,
        resolver: PermissionResolver | None = None,
        provider_id: str = "configured-provider",
    ) -> None:
        self.engine = engine
        self._store = store
        self._resolver = resolver
        self._provider_id = provider_id

    async def get_mode(self) -> str:
        return self.engine.mode.value

    async def set_mode(self, mode: str) -> str:
        self.engine.mode = PermissionMode(mode)
        await self._store.record_audit(
            PermissionAudit(
                PermissionRequest(
                    PermissionEffect.HARNESS_STATE,
                    PermissionScope("kernel", "permission.mode", "kernel"),
                ),
                self.engine.mode,
                PermissionVerdict.ALLOW,
                None,
                f"operator set permission mode to {self.engine.mode.value}",
            )
        )
        return self.engine.mode.value

    async def list_permissions(self, session_id: str) -> list[Mapping[str, object]]:
        history = await self._store.list_audit(session_id)
        return [
            {
                "tool": item.request.scope.tool_name,
                "effect": item.request.effect.value,
                "mode": item.mode.value,
                "verdict": item.verdict.value,
                "decision": item.decision.value if item.decision else None,
                "reason": item.reason,
                "scope": item.request.scope.fingerprint,
            }
            for item in history
        ]

    async def authorize_provider(self, session_id: str, workspace_root: str) -> bool:
        return await self.engine.authorize(
            PermissionRequest(
                PermissionEffect.PROVIDER_NETWORK,
                PermissionScope(
                    session_id,
                    "provider.stream",
                    workspace_root,
                    plugin_id=self._provider_id,
                ),
            ),
            resolver=self._resolver,
        )

    async def authorize_target(self, session_id: str, workspace_root: str, target: str) -> bool:
        return await self.engine.authorize(
            PermissionRequest(
                PermissionEffect.TARGET_NETWORK,
                PermissionScope(
                    session_id,
                    "target.stream",
                    workspace_root,
                    target=target,
                ),
            ),
            resolver=self._resolver,
        )
