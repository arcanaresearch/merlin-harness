from datetime import UTC, datetime, timedelta

from merlin_permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionScope,
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


def request(
    effect: PermissionEffect, *, root: str = "/one", target: str | None = None
) -> PermissionRequest:
    return PermissionRequest(effect, PermissionScope("session", "tool", root, target=target))


async def test_prompt_persists_session_rule_and_scope_change_invalidates_it() -> None:
    store = Store()
    decisions = [PermissionDecision.ALLOW_SESSION, PermissionDecision.DENY_ONCE]

    async def resolve(_request):
        return decisions.pop(0)

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)

    assert await engine.authorize(request(PermissionEffect.WORKSPACE_WRITE))
    assert await engine.authorize(request(PermissionEffect.WORKSPACE_WRITE))
    assert not await engine.authorize(request(PermissionEffect.WORKSPACE_WRITE, root="/changed"))
    assert decisions == []
    assert len(store.rules) == 1
    assert len(store.audits) == 3


def test_scope_fingerprint_covers_target_plugin_tool_workspace_and_session() -> None:
    base = PermissionScope("session", "tool", "/workspace", "plugin", "target-a")

    assert (
        base.fingerprint
        != PermissionScope("session", "tool", "/workspace", "plugin", "target-b").fingerprint
    )


def test_operation_group_shares_scope_without_erasing_audit_tool_identity() -> None:
    first = PermissionScope(
        "session",
        "browser_click",
        "/workspace",
        target="target-a",
        operation_group="browser-target-interaction",
    )
    second = PermissionScope(
        "session",
        "browser_fill",
        "/workspace",
        target="target-a",
        operation_group="browser-target-interaction",
    )

    assert first.tool_name != second.tool_name
    assert first.rule_key == second.rule_key == "browser-target-interaction"
    assert first.fingerprint == second.fingerprint


async def test_auto_yolo_and_unknown_effect_semantics() -> None:
    auto_store = Store()
    auto = PermissionEngine(PermissionMode.AUTO, auto_store)
    yolo = PermissionEngine(PermissionMode.YOLO, Store())

    assert await auto.authorize(request(PermissionEffect.WORKSPACE_READ))
    assert await auto.authorize(request(PermissionEffect.PROVIDER_NETWORK))
    assert await auto.authorize(request(PermissionEffect.TARGET_READ))
    assert not await auto.authorize(request(PermissionEffect.HARNESS_STATE))
    assert not await auto.authorize(request(PermissionEffect.WORKSPACE_WRITE))
    assert not await auto.authorize(request(PermissionEffect.TARGET_NETWORK))
    assert not await auto.authorize(request(PermissionEffect.SUBPROCESS))
    assert not await auto.authorize(request(PermissionEffect.DESTRUCTIVE))
    assert not await auto.authorize(request(PermissionEffect.UNKNOWN))
    assert await yolo.authorize(request(PermissionEffect.DESTRUCTIVE))
    assert not await yolo.authorize(request(PermissionEffect.UNKNOWN))


async def test_boolean_resolver_is_backward_compatible_allow_once() -> None:
    store = Store()

    async def legacy_resolver(_request):
        return True

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=legacy_resolver)

    assert await engine.authorize(request(PermissionEffect.SUBPROCESS))
    assert store.rules == []
    assert store.audits[0].decision is PermissionDecision.ALLOW_ONCE


async def test_expired_session_rule_is_not_inherited_by_later_workers() -> None:
    store = Store()
    requested = request(PermissionEffect.WORKSPACE_WRITE)
    store.rules.append(
        PermissionRule(
            requested.scope.session_id,
            requested.scope.tool_name,
            requested.effect,
            requested.scope.fingerprint,
            PermissionDecision.ALLOW_SESSION,
            datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    engine = PermissionEngine(PermissionMode.PROMPT, store)

    assert not await engine.authorize(requested)
    assert [audit.reason for audit in store.audits] == [
        "session rule expired",
        "operator decision required",
    ]
