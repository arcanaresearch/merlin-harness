import asyncio
from datetime import timedelta

import pytest
from merlin_permissions import (
    PermissionAudit,
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
    PermissionService,
    PermissionVerdict,
)


class Store:
    def __init__(self) -> None:
        self.rules = []
        self.audits = []

    async def find_rule(self, request):
        return next(
            (
                rule
                for rule in reversed(self.rules)
                if rule.session_id == request.scope.session_id
                and rule.tool_name == request.scope.tool_name
                and rule.effect == request.effect
                and rule.scope_fingerprint == request.scope.fingerprint
            ),
            None,
        )

    async def save_rule(self, rule) -> None:
        self.rules.append(rule)

    async def record_audit(self, audit) -> None:
        self.audits.append(audit)


def request(effect: PermissionEffect) -> PermissionRequest:
    return PermissionRequest(effect, PermissionScope("session", "tool", "/one"))


async def test_resolver_timeout_denies_and_audits_abandonment() -> None:
    store = Store()

    async def hanging_resolver(_request):
        await asyncio.sleep(60)
        return True

    engine = PermissionEngine(
        PermissionMode.PROMPT,
        store,
        resolver=hanging_resolver,
        resolver_timeout=timedelta(milliseconds=20),
    )

    assert not await engine.authorize(request(PermissionEffect.SUBPROCESS))
    abandonment = store.audits[-1]
    assert abandonment.verdict is PermissionVerdict.DENY
    assert abandonment.decision is PermissionDecision.DENY_ONCE
    assert "timed out" in abandonment.reason
    assert store.rules == []


async def test_resolver_cancellation_propagates_and_audits_abandonment() -> None:
    store = Store()
    entered = asyncio.Event()

    async def hanging_resolver(_request):
        entered.set()
        await asyncio.sleep(60)
        return True

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=hanging_resolver)
    pending = asyncio.create_task(engine.authorize(request(PermissionEffect.SUBPROCESS)))
    await asyncio.wait_for(entered.wait(), timeout=1)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    abandonment = store.audits[-1]
    assert abandonment.verdict is PermissionVerdict.DENY
    assert abandonment.decision is None
    assert "cancelled" in abandonment.reason


async def test_resolver_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="resolver timeout"):
        PermissionEngine(PermissionMode.PROMPT, Store(), resolver_timeout=timedelta(0))


async def test_set_mode_records_permission_audit() -> None:
    store = Store()
    engine = PermissionEngine(PermissionMode.PROMPT, store)
    service = PermissionService(engine, store)

    assert await service.set_mode("yolo", session_id="session") == "yolo"
    assert await service.get_mode() == "yolo"

    assert len(store.audits) == 1
    audit = store.audits[0]
    assert isinstance(audit, PermissionAudit)
    assert audit.mode is PermissionMode.YOLO
    assert audit.verdict is PermissionVerdict.ALLOW
    assert audit.request.effect is PermissionEffect.HARNESS_STATE
    assert audit.request.scope.session_id == "session"
    assert "yolo" in audit.reason
