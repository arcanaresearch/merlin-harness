from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256


class PermissionEffect(StrEnum):
    HARNESS_STATE = "harness_state"
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    PROVIDER_NETWORK = "provider_network"
    TARGET_READ = "target_read"
    TARGET_NETWORK = "target_network"
    SUBPROCESS = "subprocess"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class PermissionDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    ALLOW_COMMAND_SESSION = "allow_command_session"
    DENY_ONCE = "deny_once"
    DENY_SESSION = "deny_session"


class PermissionMode(StrEnum):
    PROMPT = "prompt"
    AUTO = "auto"
    YOLO = "yolo"


class PermissionVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


@dataclass(frozen=True, slots=True)
class PermissionScope:
    session_id: str
    tool_name: str
    workspace_root: str
    plugin_id: str | None = None
    target: str | None = None
    operation_group: str | None = None

    @property
    def rule_key(self) -> str:
        return self.operation_group or self.tool_name

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "plugin_id": self.plugin_id,
                "session_id": self.session_id,
                "target": self.target,
                "tool_name": self.rule_key,
                "workspace_root": self.workspace_root,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    effect: PermissionEffect
    scope: PermissionScope
    command_scope: PermissionScope | None = None


@dataclass(frozen=True, slots=True)
class PermissionRule:
    session_id: str
    tool_name: str
    effect: PermissionEffect
    scope_fingerprint: str
    decision: PermissionDecision
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.decision not in (
            PermissionDecision.ALLOW_SESSION,
            PermissionDecision.DENY_SESSION,
        ):
            raise ValueError("persisted rules must be session decisions")

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PermissionAudit:
    request: PermissionRequest
    mode: PermissionMode
    verdict: PermissionVerdict
    decision: PermissionDecision | None
    reason: str


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """Effect-group authority grant (durable counterpart of a session rule)."""

    id: str
    effect_classes: frozenset[PermissionEffect]
    target_scope: str | None = None
    max_concurrency: int | None = None
    evidence: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("grant id must not be empty")
        if not self.effect_classes:
            raise ValueError("grant must cover at least one effect class")
        if self.target_scope is not None and not self.target_scope.strip():
            raise ValueError("grant target scope must not be empty")
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("grant max concurrency must be positive")
        if self.evidence is not None and not self.evidence.strip():
            raise ValueError("grant evidence requirement must not be empty")

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    def admits(self, effect: PermissionEffect, *, target: str | None = None) -> bool:
        if self.expired or effect not in self.effect_classes:
            return False
        return self.target_scope is None or target == self.target_scope

    @classmethod
    def from_strings(
        cls,
        id: str,
        effect_classes: Iterable[str],
        **kwargs: object,
    ) -> CapabilityGrant:
        """Build from the merlin-contracts string-carried grant shape."""
        return cls(id, frozenset(PermissionEffect(effect) for effect in effect_classes), **kwargs)
