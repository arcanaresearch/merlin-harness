from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import (
    CapabilityGrant,
    PermissionAudit,
    PermissionDecision,
    PermissionEffect,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionVerdict,
)

# Effect-driven auto-permission distinction. Pure observation may run
# automatically; reviewed mutation (harness state, workspace writes) requires
# operator review; authority expansion, subprocess, and destructive action are
# never automatic.
_AUTO_RUNNABLE_EFFECTS = frozenset(
    {
        PermissionEffect.WORKSPACE_READ,
        PermissionEffect.PROVIDER_NETWORK,
        PermissionEffect.TARGET_READ,
    }
)


class PermissionStore(Protocol):
    async def find_rule(self, request: PermissionRequest) -> PermissionRule | None: ...

    async def save_rule(self, rule: PermissionRule) -> None: ...

    async def record_audit(self, audit: PermissionAudit) -> None: ...

    async def list_audit(self, session_id: str, *, limit: int = 100) -> list[PermissionAudit]: ...


class PermissionResolver(Protocol):
    async def __call__(self, request: PermissionRequest) -> PermissionDecision | bool: ...


class CapabilityGrantResolver(Protocol):
    async def __call__(self, grant_id: str) -> CapabilityGrant | None: ...


class PermissionEngine:
    def __init__(
        self,
        mode: PermissionMode,
        store: PermissionStore,
        *,
        resolver: PermissionResolver | None = None,
        session_rule_ttl: timedelta = timedelta(hours=8),
        resolver_timeout: timedelta = timedelta(minutes=2),
    ) -> None:
        self.mode = mode
        self._store = store
        self._resolver = resolver
        if session_rule_ttl <= timedelta(0):
            raise ValueError("session rule TTL must be positive")
        self._session_rule_ttl = session_rule_ttl
        if resolver_timeout <= timedelta(0):
            raise ValueError("resolver timeout must be positive")
        self._resolver_timeout = resolver_timeout

    async def authorize(
        self,
        request: PermissionRequest,
        *,
        resolver: PermissionResolver | None = None,
    ) -> bool:
        rule = await self._store.find_rule(request)
        if rule is not None and not rule.expired:
            allowed = rule.decision is PermissionDecision.ALLOW_SESSION
            await self._audit(
                request,
                PermissionVerdict.ALLOW if allowed else PermissionVerdict.DENY,
                rule.decision,
                "matching session rule",
            )
            return allowed
        if rule is not None:
            await self._audit(request, PermissionVerdict.PROMPT, None, "session rule expired")

        verdict = self._default(request.effect)
        if verdict is PermissionVerdict.ALLOW:
            await self._audit(request, verdict, None, f"{self.mode} mode default")
            return True
        if verdict is PermissionVerdict.DENY:
            await self._audit(request, verdict, None, "unknown effects fail closed")
            return False
        selected_resolver = resolver or self._resolver
        if selected_resolver is None:
            await self._audit(request, verdict, None, "operator decision required")
            return False

        resolved: PermissionDecision | bool
        try:
            resolved = await asyncio.wait_for(
                selected_resolver(request),
                timeout=self._resolver_timeout.total_seconds(),
            )
        except TimeoutError:
            await self._audit(
                request,
                PermissionVerdict.DENY,
                PermissionDecision.DENY_ONCE,
                "operator prompt abandoned: resolver timed out",
            )
            return False
        except asyncio.CancelledError:
            await asyncio.shield(
                self._audit(
                    request,
                    PermissionVerdict.DENY,
                    None,
                    "operator prompt abandoned: authorization cancelled",
                )
            )
            raise
        decision = (
            resolved
            if isinstance(resolved, PermissionDecision)
            else PermissionDecision.ALLOW_ONCE
            if resolved
            else PermissionDecision.DENY_ONCE
        )
        allowed = decision in (
            PermissionDecision.ALLOW_ONCE,
            PermissionDecision.ALLOW_SESSION,
        )
        if decision in (
            PermissionDecision.ALLOW_SESSION,
            PermissionDecision.DENY_SESSION,
        ):
            await self._store.save_rule(
                PermissionRule(
                    request.scope.session_id,
                    request.scope.rule_key,
                    request.effect,
                    request.scope.fingerprint,
                    decision,
                    datetime.now(UTC) + self._session_rule_ttl,
                )
            )
        await self._audit(
            request,
            PermissionVerdict.ALLOW if allowed else PermissionVerdict.DENY,
            decision,
            "operator decision",
        )
        return allowed

    async def authorize_grant(
        self,
        request: PermissionRequest,
        *,
        reason: str,
        grant: CapabilityGrant,
    ) -> bool:
        """Authorize only when typed durable authority covers effect and target scope."""
        if not grant.admits(request.effect, target=request.scope.target):
            await self._audit(
                request,
                PermissionVerdict.DENY,
                None,
                f"capability grant {grant.id} does not cover {request.effect}",
            )
            return False
        await self._audit(request, PermissionVerdict.ALLOW, None, reason)
        return True

    def _default(self, effect: PermissionEffect) -> PermissionVerdict:
        if effect is PermissionEffect.UNKNOWN:
            return PermissionVerdict.DENY
        if self.mode is PermissionMode.YOLO:
            return PermissionVerdict.ALLOW
        if self.mode is PermissionMode.AUTO and effect in _AUTO_RUNNABLE_EFFECTS:
            return PermissionVerdict.ALLOW
        return PermissionVerdict.PROMPT

    async def _audit(
        self,
        request: PermissionRequest,
        verdict: PermissionVerdict,
        decision: PermissionDecision | None,
        reason: str,
    ) -> None:
        await self._store.record_audit(
            PermissionAudit(request, self.mode, verdict, decision, reason)
        )
