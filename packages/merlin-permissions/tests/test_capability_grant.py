from datetime import UTC, datetime, timedelta

import pytest
from merlin_permissions import (
    CapabilityGrant,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
    PermissionVerdict,
)


class Store:
    def __init__(self) -> None:
        self.rules = []
        self.audits = []

    async def find_rule(self, request):
        return None

    async def save_rule(self, rule) -> None:
        self.rules.append(rule)

    async def record_audit(self, audit) -> None:
        self.audits.append(audit)


def request(effect: PermissionEffect, *, target: str | None = None) -> PermissionRequest:
    return PermissionRequest(effect, PermissionScope("session", "tool", "/one", target=target))


def test_capability_grant_from_strings_converts_effects() -> None:
    grant = CapabilityGrant.from_strings(
        "grant-1", ["target_read", "provider_network"], target_scope="target-9"
    )
    assert grant.effect_classes == frozenset(
        {PermissionEffect.TARGET_READ, PermissionEffect.PROVIDER_NETWORK}
    )
    assert grant.admits(PermissionEffect.TARGET_READ, target="target-9")
    assert not grant.admits(PermissionEffect.TARGET_READ, target="other")
    assert not grant.admits(PermissionEffect.WORKSPACE_WRITE, target="target-9")


def test_capability_grant_expiry() -> None:
    expired = CapabilityGrant(
        id="g",
        effect_classes=frozenset({PermissionEffect.TARGET_READ}),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert expired.expired
    assert not expired.admits(PermissionEffect.TARGET_READ)
    live = CapabilityGrant(
        id="g",
        effect_classes=frozenset({PermissionEffect.TARGET_READ}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert not live.expired
    assert live.admits(PermissionEffect.TARGET_READ)


def test_authorize_grant_requires_typed_grant() -> None:
    store = Store()
    engine = PermissionEngine(PermissionMode.PROMPT, store)
    with pytest.raises(TypeError):
        engine.authorize_grant(  # type: ignore[call-arg]
            request(PermissionEffect.WORKSPACE_WRITE), reason="durable authority"
        )
    assert store.audits == []


async def test_authorize_grant_enforces_capability_grant() -> None:
    store = Store()
    engine = PermissionEngine(PermissionMode.PROMPT, store)
    grant = CapabilityGrant.from_strings("grant-1", ["target_read"], target_scope="t1")

    assert await engine.authorize_grant(
        request(PermissionEffect.TARGET_READ, target="t1"), reason="grant", grant=grant
    )
    assert store.audits[-1].verdict is PermissionVerdict.ALLOW

    assert not await engine.authorize_grant(
        request(PermissionEffect.TARGET_NETWORK, target="t1"), reason="grant", grant=grant
    )
    assert store.audits[-1].verdict is PermissionVerdict.DENY

    assert not await engine.authorize_grant(
        request(PermissionEffect.TARGET_READ, target="t2"), reason="grant", grant=grant
    )
    assert store.audits[-1].verdict is PermissionVerdict.DENY


@pytest.mark.parametrize(
    "kwargs",
    (
        {"target_scope": ""},
        {"max_concurrency": 0},
        {"evidence": ""},
    ),
)
def test_capability_grant_rejects_invalid_constraints(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CapabilityGrant(
            id="g",
            effect_classes=frozenset({PermissionEffect.TARGET_READ}),
            **kwargs,
        )
