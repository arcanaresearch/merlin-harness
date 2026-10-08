from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from merlin_permissions import (
    PermissionAudit,
    PermissionDecision,
    PermissionEffect,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    PermissionVerdict,
)


class PermissionRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def find_rule(self, request: PermissionRequest) -> PermissionRule | None:
        row = self.connection.execute(
            """SELECT session_id, tool_name, effect, scope_fingerprint, decision, expires_at
               FROM merlin_storage.permission_rules
               WHERE session_id = %s AND tool_name = %s AND effect = %s
                 AND scope_fingerprint = %s""",
            (
                request.scope.session_id,
                request.scope.rule_key,
                request.effect.value,
                request.scope.fingerprint,
            ),
        ).fetchone()
        if row is None:
            return None
        return PermissionRule(
            str(row["session_id"]),
            row["tool_name"],
            PermissionEffect(row["effect"]),
            row["scope_fingerprint"],
            PermissionDecision(row["decision"]),
            row["expires_at"],
        )

    def save_rule(self, rule: PermissionRule) -> None:
        self.connection.execute(
            """INSERT INTO merlin_storage.permission_rules
                   (id, session_id, tool_name, effect, scope_fingerprint, decision, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (session_id, tool_name, effect, scope_fingerprint)
               DO UPDATE SET decision = excluded.decision, expires_at = excluded.expires_at,
                             created_at = now()""",
            (
                uuid4(),
                rule.session_id,
                rule.tool_name,
                rule.effect.value,
                rule.scope_fingerprint,
                rule.decision.value,
                rule.expires_at,
            ),
        )
        self.connection.commit()

    def record_audit(self, audit: PermissionAudit) -> None:
        scope = audit.request.scope
        self.connection.execute(
            """INSERT INTO merlin_storage.permission_audit
                   (id, session_id, tool_name, effect, scope_fingerprint, workspace_root,
                    plugin_id, target, operation_group, mode, verdict, decision, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                scope.session_id,
                scope.tool_name,
                audit.request.effect.value,
                scope.fingerprint,
                scope.workspace_root,
                scope.plugin_id,
                scope.target,
                scope.operation_group,
                audit.mode.value,
                audit.verdict.value,
                audit.decision.value if audit.decision else None,
                audit.reason,
            ),
        )
        self.connection.commit()

    def list_audit(self, session_id: str, *, limit: int = 100) -> list[PermissionAudit]:
        rows = self.connection.execute(
            """SELECT session_id, tool_name, effect, workspace_root, plugin_id, target,
                      operation_group, mode, verdict, decision, reason
               FROM merlin_storage.permission_audit WHERE session_id = %s
               ORDER BY created_at DESC, id DESC LIMIT %s""",
            (session_id, limit),
        ).fetchall()
        from merlin_permissions import PermissionScope

        return [
            PermissionAudit(
                PermissionRequest(
                    PermissionEffect(row["effect"]),
                    PermissionScope(
                        str(row["session_id"]),
                        row["tool_name"],
                        row["workspace_root"],
                        row["plugin_id"],
                        row["target"],
                        row["operation_group"],
                    ),
                ),
                PermissionMode(row["mode"]),
                PermissionVerdict(row["verdict"]),
                PermissionDecision(row["decision"]) if row["decision"] else None,
                row["reason"],
            )
            for row in rows
        ]


class PostgresPermissionStore:
    def __init__(self, repository: PermissionRepository) -> None:
        self._repository = repository

    async def find_rule(self, request: PermissionRequest) -> PermissionRule | None:
        return await asyncio.to_thread(self._repository.find_rule, request)

    async def save_rule(self, rule: PermissionRule) -> None:
        await asyncio.to_thread(self._repository.save_rule, rule)

    async def record_audit(self, audit: PermissionAudit) -> None:
        await asyncio.to_thread(self._repository.record_audit, audit)

    async def list_audit(self, session_id: str, *, limit: int = 100) -> list[PermissionAudit]:
        return await asyncio.to_thread(self._repository.list_audit, session_id, limit=limit)
