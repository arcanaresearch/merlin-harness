from datetime import UTC, datetime, timedelta

import pytest
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


class CommandStore(Store):
    async def find_rule(self, request):
        return next(
            (
                rule
                for rule in reversed(self.rules)
                if rule.session_id == request.scope.session_id
                and rule.tool_name == request.scope.rule_key
                and rule.effect == request.effect
                and rule.scope_fingerprint == request.scope.fingerprint
            ),
            None,
        )


def command_request(
    command: str = "command-a", *, root: str = "/one", target: str | None = None
) -> PermissionRequest:
    scope = PermissionScope("session", "shell_command", root, target=target)
    command_scope = PermissionScope(
        "session", "shell_command", root, target=target, operation_group=command
    )
    return PermissionRequest(PermissionEffect.SUBPROCESS, scope, command_scope)


async def test_command_session_rule_reuses_only_exact_command_scope() -> None:
    store = CommandStore()
    decisions = [PermissionDecision.ALLOW_COMMAND_SESSION] + [PermissionDecision.DENY_ONCE] * 3

    async def resolve(_request):
        return decisions.pop(0)

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)
    requested = command_request()

    assert await engine.authorize(requested)
    assert await engine.authorize(requested)
    assert not await engine.authorize(command_request("command-b"))
    assert not await engine.authorize(command_request(root="/changed"))
    assert not await engine.authorize(command_request(target="another-target"))
    assert not decisions
    assert len(store.rules) == 1
    assert store.rules[0].decision is PermissionDecision.ALLOW_SESSION
    assert store.rules[0].scope_fingerprint == requested.command_scope.fingerprint
    assert store.rules[0].tool_name == "command-a"
    assert store.rules[0].expires_at > datetime.now(UTC)
    assert [audit.decision for audit in store.audits[:2]] == [
        PermissionDecision.ALLOW_COMMAND_SESSION,
        PermissionDecision.ALLOW_COMMAND_SESSION,
    ]
    assert all(audit.request.scope == requested.command_scope for audit in store.audits[:2])
    assert store.audits[1].reason == "matching command session rule"


async def test_broad_session_deny_overrides_exact_command_allow() -> None:
    store = CommandStore()

    async def resolve(_request):
        return PermissionDecision.ALLOW_COMMAND_SESSION

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)
    requested = command_request()
    assert await engine.authorize(requested)
    store.rules.append(
        PermissionRule(
            requested.scope.session_id,
            requested.scope.rule_key,
            requested.effect,
            requested.scope.fingerprint,
            PermissionDecision.DENY_SESSION,
        )
    )

    assert not await engine.authorize(requested)
    assert store.audits[-1].decision is PermissionDecision.DENY_SESSION
    assert store.audits[-1].reason == "matching session rule"


async def test_expired_command_session_rule_requires_new_approval() -> None:
    store = CommandStore()
    requested = command_request()
    scope = requested.command_scope
    store.rules.append(
        PermissionRule(
            scope.session_id,
            scope.rule_key,
            requested.effect,
            scope.fingerprint,
            PermissionDecision.ALLOW_SESSION,
            datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    engine = PermissionEngine(PermissionMode.PROMPT, store)

    assert not await engine.authorize(requested)
    assert [audit.reason for audit in store.audits] == [
        "command session rule expired",
        "operator decision required",
    ]


async def test_expired_broad_deny_falls_back_to_exact_command_allow() -> None:
    store = CommandStore()

    async def resolve(_request):
        return PermissionDecision.ALLOW_COMMAND_SESSION

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)
    requested = command_request()
    assert await engine.authorize(requested)
    store.rules.append(
        PermissionRule(
            requested.scope.session_id,
            requested.scope.rule_key,
            requested.effect,
            requested.scope.fingerprint,
            PermissionDecision.DENY_SESSION,
            datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    assert await engine.authorize(requested)
    assert [audit.reason for audit in store.audits[-2:]] == [
        "session rule expired",
        "matching command session rule",
    ]


async def test_command_session_decision_without_exact_scope_denies_without_persisting() -> None:
    store = CommandStore()

    async def resolve(_request):
        return PermissionDecision.ALLOW_COMMAND_SESSION

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)

    assert not await engine.authorize(request(PermissionEffect.SUBPROCESS))
    assert not store.rules
    assert store.audits[-1].reason == "command session decision requires an exact command scope"


@pytest.mark.parametrize(
    "decision", [PermissionDecision.ALLOW_SESSION, PermissionDecision.DENY_SESSION]
)
async def test_existing_broad_session_decisions_still_cover_different_commands(decision) -> None:
    store = CommandStore()
    calls = 0

    async def resolve(_request):
        nonlocal calls
        calls += 1
        return decision

    engine = PermissionEngine(PermissionMode.PROMPT, store, resolver=resolve)
    expected = decision is PermissionDecision.ALLOW_SESSION

    assert await engine.authorize(command_request()) is expected
    assert await engine.authorize(command_request("command-b")) is expected
    assert calls == 1
    assert len(store.rules) == 1
