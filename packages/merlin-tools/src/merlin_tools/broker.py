from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for
from merlin_contracts import (
    LEGACY_TARGET_EXCHANGE_TOOL_IDS,
    InfrastructureFailureCategory,
    TargetDeliveryClass,
    Tool,
    ToolContext,
    ToolEffectRecord,
    ToolEffectState,
    ToolInteractionRole,
    ToolResult,
    ToolSpec,
)
from merlin_permissions import (
    CapabilityGrant,
    PermissionDecision,
    PermissionEffect,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionScope,
)

from .delivery_receipt import target_delivery_receipt
from .read_only import prepare_read_only_command


class ToolConfirmer(Protocol):
    async def __call__(
        self,
        spec: ToolSpec,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> bool | PermissionDecision: ...


class PermissionScopeResolver(Protocol):
    def __call__(
        self,
        tool_name: str,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> PermissionScope: ...


class ToolEffectStore(Protocol):
    async def begin(self, record: ToolEffectRecord) -> tuple[ToolEffectRecord, bool]: ...

    async def finish(self, record: ToolEffectRecord) -> ToolEffectRecord: ...

    async def list_for_session(
        self, session_id: str, *, tool_name: str | None = None
    ) -> list[ToolEffectRecord]: ...

    async def list_for_attempt(
        self, attempt_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]: ...

    async def list_for_campaign(
        self, campaign_id: str, *, limit: int | None = None
    ) -> list[ToolEffectRecord]: ...


class DurableGrantResolver(Protocol):
    async def __call__(
        self, tool_name: str, context: ToolContext, effect: PermissionEffect
    ) -> CapabilityGrant | None: ...


def _infrastructure_failure_category(
    error: BaseException,
) -> InfrastructureFailureCategory | None:
    from merlin_contracts.storage_failures import storage_failure_category

    if category := storage_failure_category(error):
        return category
    typed = getattr(error, "infrastructure_failure_category", None)
    if isinstance(typed, InfrastructureFailureCategory):
        return typed
    if isinstance(typed, str):
        try:
            return InfrastructureFailureCategory(typed)
        except ValueError:
            pass
    if isinstance(error, TimeoutError):
        return InfrastructureFailureCategory.ADAPTER_TIMEOUT
    message = " ".join(str(error).casefold().split())
    if "response pointer missing" in message:
        return InfrastructureFailureCategory.MISSING_RESPONSE_POINTER
    if "session" in message and any(
        marker in message
        for marker in ("ambiguous", "closed", "expired", "invalid", "unavailable", "unhealthy")
    ):
        return InfrastructureFailureCategory.UNHEALTHY_TARGET_SESSION
    return None


def _shell_command_scope(
    tool: Tool,
    effect: PermissionEffect,
    scope: PermissionScope,
    context: ToolContext,
    arguments: Mapping[str, Any],
) -> PermissionScope | None:
    # Import at invocation time: workspace tools themselves import this broker.
    from .workspace import ShellCommandTool

    if type(tool) is not ShellCommandTool or effect is not PermissionEffect.SUBPROCESS:
        return None
    argv = arguments.get("argv")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 128
        or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
    ):
        return None
    value = arguments.get("cwd", ".")
    if not isinstance(value, str):
        return None
    try:
        relative = Path(value or ".")
        if relative.is_absolute():
            return None
        root = Path(context.workspace_root).resolve(strict=True)
        cwd = (root / relative).resolve(strict=True)
        cwd.relative_to(root)
        if not cwd.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    # Match the executor's resolved directory and the complete, unredacted argv.
    # Preserve the existing workspace identity and all other authority boundaries.
    payload = json.dumps({"argv": argv, "cwd": str(cwd)}, sort_keys=True, separators=(",", ":"))
    digest = sha256(payload.encode()).hexdigest()
    return replace(scope, operation_group=f"{scope.rule_key}:command:{digest}")


class ToolBroker:
    """The single invocation boundary for all registered tools."""

    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        confirmer: ToolConfirmer | None = None,
        permissions: PermissionEngine | None = None,
        permission_scope: PermissionScopeResolver | None = None,
        effect_store: ToolEffectStore | None = None,
        durable_grant: DurableGrantResolver | None = None,
        max_argument_bytes: int = 256 * 1024,
        max_result_bytes: int = 1024 * 1024,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._confirmer = confirmer
        self._permissions = permissions
        self._permission_scope = permission_scope
        self._effect_store = effect_store
        self._durable_grant = durable_grant
        self._max_argument_bytes = max_argument_bytes
        self._max_result_bytes = max_result_bytes
        if max_argument_bytes < 1 or max_result_bytes < 1:
            raise ValueError("tool-call bounds must be positive")
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.describe().name
        if not name:
            raise ValueError("tool name cannot be empty")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def unregister(self, name: str) -> Tool | None:
        """Remove a tool, returning it when it was registered."""

        return self._tools.pop(name, None)

    def replace_by_prefix(self, prefix: str, tools: Iterable[Tool]) -> None:
        """Atomically replace the tools owned by a namespace prefix."""

        replacements: dict[str, Tool] = {}
        for tool in tools:
            name = tool.describe().name
            if not name.startswith(prefix):
                raise ValueError(f"tool name must start with {prefix!r}: {name}")
            if name in replacements:
                raise ValueError(f"tool already registered: {name}")
            replacements[name] = tool

        retained = {name: tool for name, tool in self._tools.items() if not name.startswith(prefix)}
        collision = next((name for name in replacements if name in retained), None)
        if collision is not None:
            raise ValueError(f"tool already registered: {collision}")
        self._tools = retained | replacements

    def describe(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.describe() for tool in self._tools.values())

    def effect(self, name: str) -> str | None:
        """Return only a tool's explicitly declared effect classification."""

        tool = self._tools.get(name)
        declared = getattr(tool, "effect", None) if tool is not None else None
        return declared if isinstance(declared, str) and declared else None

    def interaction_role(self, name: str) -> ToolInteractionRole | None:
        """Return a tool's declared semantic interaction role."""

        tool = self._tools.get(name)
        if tool is None:
            return None
        declared = tool.describe().interaction_role
        if declared is ToolInteractionRole.TARGET_EXCHANGE:
            return declared
        if name in LEGACY_TARGET_EXCHANGE_TOOL_IDS:
            # Compatibility for target tools authored before interaction roles.
            return ToolInteractionRole.TARGET_EXCHANGE
        return declared

    def target_exchange_tool_ids(self, names: Iterable[str]) -> tuple[str, ...]:
        """Resolve primary target exchanges without inferring semantics from tool names."""

        candidates = tuple(names)
        declared = tuple(
            name
            for name in candidates
            if self.interaction_role(name) is ToolInteractionRole.TARGET_EXCHANGE
        )
        if declared:
            return declared
        # Compatibility for callers registering pre-role target tools.
        return tuple(name for name in candidates if name in LEGACY_TARGET_EXCHANGE_TOOL_IDS)

    async def list_effects_for_attempt(
        self, attempt_id: str, *, limit: int | None = None
    ) -> tuple[ToolEffectRecord, ...]:
        """Expose immutable durable effects for evidence resolution, when configured."""
        if self._effect_store is None:
            return ()
        loader = getattr(self._effect_store, "list_for_attempt", None)
        if not callable(loader):
            return ()
        if limit is None:
            return tuple(await loader(attempt_id))
        return tuple(await loader(attempt_id, limit=limit))

    async def list_effects_for_campaign(
        self, campaign_id: str, *, limit: int | None = None
    ) -> tuple[ToolEffectRecord, ...]:
        """Expose immutable durable effects for one campaign's read projection."""
        if self._effect_store is None:
            return ()
        loader = getattr(self._effect_store, "list_for_campaign", None)
        if not callable(loader):
            return ()
        return tuple(await loader(campaign_id, limit=limit))

    async def invoke(
        self,
        name: str,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(content=f"unknown tool: {name}", is_error=True)
        spec = tool.describe()
        interaction_role = self.interaction_role(name) or ToolInteractionRole.AUXILIARY
        is_target_exchange = interaction_role is ToolInteractionRole.TARGET_EXCHANGE
        try:
            encoded_arguments = json.dumps(
                arguments, sort_keys=True, separators=(",", ":")
            ).encode()
            encoded_effect_arguments = _encode_effect_arguments(arguments)
        except (TypeError, ValueError) as error:
            return ToolResult(content=f"invalid tool arguments: {error}", is_error=True)
        if len(encoded_arguments) > self._max_argument_bytes:
            return ToolResult(
                content=f"tool arguments exceed {self._max_argument_bytes} byte limit",
                is_error=True,
                metadata={"arguments_too_large": True},
            )
        try:
            validator = validator_for(spec.input_schema)
            validator.check_schema(spec.input_schema)
            validator(spec.input_schema).validate(arguments)
        except SchemaError:
            return ToolResult(
                content=f"invalid input schema advertised by tool: {name}",
                is_error=True,
                metadata={"tool_schema_invalid": True},
            )
        except ValidationError as error:
            path = ".".join(str(part) for part in error.absolute_path)
            location = f" at {path}" if path else ""
            return ToolResult(
                content=f"invalid arguments for tool {name}{location}: {error.message}",
                is_error=True,
                metadata={"schema_validation_failed": True},
            )
        from .workspace import ShellCommandTool

        prepared_read_only = None
        if type(tool) is ShellCommandTool:
            # Snapshot caller-owned argv before authorization yields. Only this
            # built-in executor may claim the read-only subprocess distinction.
            arguments = dict(arguments) | {"argv": list(arguments["argv"])}
        effect_name = self.effect(name) or PermissionEffect.UNKNOWN.value
        try:
            effect = PermissionEffect(effect_name)
        except ValueError:
            effect = PermissionEffect.UNKNOWN
        evaluation_uncertainties: list[dict[str, object]] = []
        actual_target_digest = sha256(encoded_effect_arguments).hexdigest()
        planned_actions: set[tuple[str, str]] = set()
        if is_target_exchange and context.target_effects_blocked:
            evaluation_uncertainties.append(
                {
                    "source": "baseline_predecessor",
                    "reason": (
                        "predecessor was not qualified; the authorized target action "
                        "continued and requires evidence-based validation"
                    ),
                    "confidence": 1.0,
                    "retryable": True,
                }
            )
        if is_target_exchange and context.expected_target_sequence:
            planned_actions = {
                (planned_name, planned_digest.removeprefix("sha256:"))
                for planned_name, planned_digest in context.expected_target_sequence
            }
            if (name, actual_target_digest) not in planned_actions:
                evaluation_uncertainties.append(
                    {
                        "source": "planned_target_sequence",
                        "reason": (
                            "authorized target action differed from the preplanned sequence; "
                            "it was recorded as a new effect"
                        ),
                        "confidence": 1.0,
                        "retryable": False,
                    }
                )
        elif (
            is_target_exchange
            and context.expected_target_arguments_digest is not None
            and (
                name != context.expected_target_tool
                or actual_target_digest
                != context.expected_target_arguments_digest.removeprefix("sha256:")
            )
        ):
            evaluation_uncertainties.append(
                {
                    "source": "planned_target_action",
                    "reason": (
                        "authorized target action differed from its planned tool or arguments; "
                        "it was recorded as a new effect"
                    ),
                    "confidence": 1.0,
                    "retryable": False,
                }
            )
        if self._permissions is not None:
            scope = (
                self._permission_scope(name, context, arguments)
                if self._permission_scope is not None
                else self._default_scope(name, context, arguments)
            )
            command_scope = _shell_command_scope(tool, effect, scope, context, arguments)
            if command_scope is not None and self._permissions.mode is PermissionMode.AUTO:
                prepared_read_only = await prepare_read_only_command(context, arguments)

            async def resolve(
                _request: PermissionRequest,
            ) -> bool | PermissionDecision:
                if self._confirmer is None:
                    return False
                permission_arguments = dict(arguments)
                permission_arguments.update(
                    {
                        "__permission_effect": effect.value,
                        "__permission_target": scope.target,
                        "__permission_plugin": scope.plugin_id,
                        "__permission_group": scope.operation_group,
                        "__permission_command_scope": command_scope is not None,
                    }
                )
                return await self._confirmer(spec, context, permission_arguments)

            request = PermissionRequest(
                effect,
                scope,
                command_scope=command_scope,
                read_only_command=prepared_read_only is not None,
            )
            grant = (
                await self._durable_grant(name, context, effect)
                if self._durable_grant is not None
                else None
            )
            if grant is not None and not isinstance(grant, CapabilityGrant):
                return ToolResult(
                    content="durable capability resolver returned an invalid grant",
                    is_error=True,
                    metadata={
                        "effect": effect.value,
                        "invalid_durable_grant": True,
                    },
                )
            allowed = (
                await self._permissions.authorize_grant(
                    request,
                    reason="verified durable campaign capability grant",
                    grant=grant,
                )
                if grant is not None
                else await self._permissions.authorize(request, resolver=resolve)
            )
            if not allowed:
                return ToolResult(
                    content=f"permission denied for tool: {name}",
                    is_error=True,
                    metadata={"effect": effect.value, "permission_denied": True},
                )
            if command_scope is not None:
                current_command_scope = _shell_command_scope(
                    tool, effect, scope, context, arguments
                )
                if current_command_scope != command_scope:
                    return ToolResult(
                        content=(
                            "command or working directory changed during permission approval; "
                            "retry to review the current execution details"
                        ),
                        is_error=True,
                        metadata={
                            "effect": effect.value,
                            "permission_scope_changed": True,
                            "retry_required": True,
                        },
                    )
        elif effect is PermissionEffect.TARGET_NETWORK or (
            spec.name.startswith("mcp__") and effect is PermissionEffect.UNKNOWN
        ):
            if self._confirmer is None or not await self._confirmer(spec, context, arguments):
                return ToolResult(
                    content=f"confirmation denied for tool: {name}",
                    is_error=True,
                    metadata={"effect": effect.value, "confirmation_required": True},
                )
        intent: ToolEffectRecord | None = None
        if self._effect_store is not None and context.call_id is not None and self._durable(effect):
            digest = sha256(encoded_arguments).hexdigest()
            ownership = context.attempt_id or context.agent_id or "legacy"
            lineage_loader = getattr(self._effect_store, "list_for_attempt", None)
            if context.effect_attempt_lineage and lineage_loader is not None:
                ancestor_effects = [
                    item
                    for lineage_attempt_id in context.effect_attempt_lineage[:-1]
                    for item in await lineage_loader(lineage_attempt_id)
                ]
                if (
                    is_target_exchange
                    and context.expected_target_sequence
                    and any(
                        item.effect == PermissionEffect.TARGET_NETWORK.value
                        and (
                            item.tool_name,
                            item.arguments_digest,
                        )
                        not in planned_actions
                        for item in ancestor_effects
                    )
                ):
                    evaluation_uncertainties.append(
                        {
                            "source": "durable_target_history",
                            "reason": (
                                "durable target history differed from the preplanned sequence; "
                                "fresh authorized effects remained executable"
                            ),
                            "confidence": 1.0,
                            "retryable": False,
                        }
                    )

                def same_effect_arguments(item: ToolEffectRecord) -> bool:
                    if not is_target_exchange:
                        return item.arguments_digest == digest
                    recorded = item.result_metadata.get("target_arguments_digest")
                    if isinstance(recorded, str):
                        return recorded.removeprefix("sha256:") == actual_target_digest
                    if isinstance(item.arguments_payload, Mapping):
                        encoded = _encode_effect_arguments(item.arguments_payload)
                        return sha256(encoded).hexdigest() == actual_target_digest
                    return item.arguments_digest == digest

                matching_ancestors = tuple(
                    item
                    for item in ancestor_effects
                    if item.effect == effect.value
                    and item.session_id == context.session_id
                    and item.tool_name == name
                    and same_effect_arguments(item)
                    and (context.target_id is None or item.target_id == context.target_id)
                    and (context.campaign_id is None or item.campaign_id == context.campaign_id)
                )
                prior = matching_ancestors[0] if len(matching_ancestors) == 1 else None
                if prior is not None:
                    if prior.state in {
                        ToolEffectState.ACKNOWLEDGED,
                        ToolEffectState.RECONCILED,
                    }:
                        return ToolResult(
                            content=prior.result_content or "",
                            is_error=bool(prior.result_is_error),
                            metadata=prior.result_metadata
                            | {
                                "idempotent_replacement_adoption": True,
                                **(
                                    {"evaluation_uncertain": evaluation_uncertainties}
                                    if evaluation_uncertainties
                                    else {}
                                ),
                            },
                        )
                    return ToolResult(
                        content="ancestor target effect is unresolved; replacement cannot resend",
                        is_error=True,
                        metadata={"effect_ambiguous": True, "replacement_blocked": True},
                    )
            key = sha256(
                f"{context.session_id}\0{ownership}\0{context.call_id}\0{name}".encode()
            ).hexdigest()
            proposed = ToolEffectRecord(
                idempotency_key=key,
                session_id=context.session_id,
                call_id=context.call_id,
                agent_id=context.agent_id,
                attempt_id=context.attempt_id,
                campaign_id=context.campaign_id,
                target_id=context.target_id,
                tool_name=name,
                effect=effect.value,
                arguments_digest=digest,
                arguments_payload=dict(arguments),
                state=ToolEffectState.INTENT,
            )
            intent, created = await self._effect_store.begin(proposed)
            if not created:
                if intent.state in (ToolEffectState.ACKNOWLEDGED, ToolEffectState.RECONCILED):
                    return ToolResult(
                        content=intent.result_content or "",
                        is_error=bool(intent.result_is_error),
                        metadata=intent.result_metadata | {"idempotent_replay": True},
                    )
                if intent.state is ToolEffectState.INTENT:
                    intent = intent.model_copy(
                        update={
                            "state": ToolEffectState.AMBIGUOUS,
                            "reconciliation": "process stopped after intent; target effect unknown",
                        }
                    )
                    await self._effect_store.finish(intent)
                return ToolResult(
                    content="tool effect is unresolved; reconcile it before retrying",
                    is_error=True,
                    metadata={"effect_ambiguous": True, "idempotency_key": key},
                )
        try:
            result = await tool.invoke(context, prepared_read_only or arguments)
        except asyncio.CancelledError:
            if intent is not None:
                await self._effect_store.finish(
                    intent.model_copy(
                        update={
                            "state": ToolEffectState.AMBIGUOUS,
                            "reconciliation": "tool invocation cancelled after effect intent",
                        }
                    )
                )
            raise
        except Exception as error:
            failure_category = _infrastructure_failure_category(error)
            if not isinstance(error, (OSError, TypeError, ValueError)) and failure_category is None:
                raise
            failure_metadata = (
                {"infrastructure_failure_category": failure_category.value}
                if failure_category is not None
                else {}
            )
            if intent is not None:
                await self._effect_store.finish(
                    intent.model_copy(
                        update={
                            "state": ToolEffectState.AMBIGUOUS,
                            "reconciliation": str(error),
                        }
                    )
                )
                return ToolResult(
                    content=str(error),
                    is_error=True,
                    metadata={
                        "effect_ambiguous": True,
                        "idempotency_key": intent.idempotency_key,
                        **failure_metadata,
                    },
                )
            return ToolResult(content=str(error), is_error=True, metadata=failure_metadata)
        result = self._bound_result(result)
        if evaluation_uncertainties:
            existing = result.metadata.get("evaluation_uncertain")
            retained = (
                [dict(item) for item in existing if isinstance(item, Mapping)]
                if isinstance(existing, (list, tuple))
                else []
            )
            result = result.model_copy(
                update={
                    "metadata": result.metadata
                    | {"evaluation_uncertain": retained + evaluation_uncertainties}
                }
            )
        if effect is PermissionEffect.TARGET_NETWORK:
            result = result.model_copy(
                update={"metadata": result.metadata | {"interaction_role": interaction_role.value}}
            )
        if effect is PermissionEffect.TARGET_NETWORK:
            result = result.model_copy(
                update={
                    "metadata": result.metadata
                    | {
                        "target_arguments_digest": (
                            "sha256:" + sha256(encoded_effect_arguments).hexdigest()
                        )
                    }
                }
            )
        if context.technique_attempt_id is not None:
            result = result.model_copy(
                update={
                    "metadata": result.metadata
                    | {"technique_attempt_id": context.technique_attempt_id}
                }
            )
        if (
            not result.is_error
            and context.technique_attempt_id is not None
            and effect is PermissionEffect.TARGET_NETWORK
            and is_target_exchange
        ):
            actual_delivery = spec.target_delivery
            if actual_delivery is None and spec.target_delivery_from_reviewed_adapter:
                declared_delivery = result.metadata.get("reviewed_target_delivery")
                if isinstance(declared_delivery, str):
                    try:
                        actual_delivery = TargetDeliveryClass(declared_delivery)
                    except ValueError:
                        actual_delivery = None
            delivery = target_delivery_receipt(
                arguments,
                context,
                transport_id=name,
                actual_delivery=actual_delivery,
            )
            result = result.model_copy(
                update={"metadata": result.metadata | {"target_delivery_receipt": asdict(delivery)}}
            )
        if context.transform_bindings and effect is PermissionEffect.TARGET_NETWORK:
            result = result.model_copy(
                update={
                    "metadata": result.metadata
                    | {
                        "transform_bindings": [list(item) for item in context.transform_bindings],
                        "concrete_payload_arguments_digest": (
                            "sha256:" + sha256(encoded_arguments).hexdigest()
                        ),
                    }
                }
            )
        if intent is not None:
            await self._effect_store.finish(
                intent.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": result.content,
                        "result_is_error": result.is_error,
                        "result_metadata": result.metadata,
                    }
                )
            )
        return result

    @staticmethod
    def _durable(effect: PermissionEffect) -> bool:
        return effect in {
            PermissionEffect.HARNESS_STATE,
            PermissionEffect.WORKSPACE_WRITE,
            PermissionEffect.TARGET_READ,
            PermissionEffect.TARGET_NETWORK,
            PermissionEffect.SUBPROCESS,
            PermissionEffect.DESTRUCTIVE,
        }

    def _bound_result(self, result: ToolResult) -> ToolResult:
        encoded = result.content.encode()
        if len(encoded) <= self._max_result_bytes:
            return result
        content = encoded[: self._max_result_bytes].decode(errors="ignore")
        return result.model_copy(
            update={
                "content": content,
                "metadata": result.metadata
                | {"result_truncated": True, "original_bytes": len(encoded)},
            }
        )

    @staticmethod
    def _default_scope(
        name: str, context: ToolContext, arguments: Mapping[str, Any]
    ) -> PermissionScope:
        plugin_id = name.split("__", 2)[1] if name.startswith("mcp__") else None
        target = context.target_id
        if target is None:
            candidate = arguments.get("target") or arguments.get("target_id")
            if candidate is None and name.startswith("browser_"):
                candidate = arguments.get("url")
            target = str(candidate) if candidate is not None else None
        operation_group = None
        if name.startswith("browser_"):
            operation_group = "browser-target-interaction"
        elif plugin_id is not None:
            operation_group = f"mcp:{plugin_id}"
        return PermissionScope(
            session_id=context.session_id,
            tool_name=name,
            workspace_root=context.workspace_root,
            plugin_id=plugin_id,
            target=target,
            operation_group=operation_group,
        )


def _encode_effect_arguments(arguments: Mapping[str, Any]) -> bytes:
    """Encode the target-bearing identity while excluding advisory model metadata."""
    identity = {
        key: value
        for key, value in arguments.items()
        if key not in {"strategy_plan", "strategy_turn_index"}
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
