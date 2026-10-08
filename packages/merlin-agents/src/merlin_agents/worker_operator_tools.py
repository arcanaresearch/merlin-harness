"""Session-scoped foreground tools for durable worker guidance."""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_contracts.agents import AgentSummary

from merlin_agents.mailbox import DeliveryMode

_MESSAGEABLE = frozenset({"queued", "ready", "leased", "running"})


class OperatorWorkerService(Protocol):
    async def list_agents(self, session_id: str) -> Sequence[AgentSummary]: ...

    async def message_agent_receipt_exists(
        self, session_id: str, agent_id: str, message: str, *, schedule_id: str, message_id: str
    ) -> bool: ...

    async def message_schedule_agent_once(
        self,
        schedule_id: str,
        session_id: str,
        agent_id: str,
        message: str,
        *,
        message_id: str,
        mode: DeliveryMode = DeliveryMode.FOLLOW_UP,
        sender_id: str | None = None,
    ) -> bool: ...


class AgentListTool:
    effect = "workspace_read"

    def __init__(self, service: OperatorWorkerService) -> None:
        self._service = service

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="agent_list",
            description=(
                "Discover worker IDs, names, campaigns, and current states owned by this "
                "conversation before forwarding operator guidance. Use exact returned IDs; "
                "ask the operator when multiple workers match their requested recipient."
            ),
            input_schema={
                "type": "object",
                "properties": {"campaign_id": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        if context.agent_id is not None or context.campaign_id is not None:
            return ToolResult(
                content="operator messaging is available only to foreground chat", is_error=True
            )
        campaign_id = arguments.get("campaign_id")
        if set(arguments) - {"campaign_id"} or (
            campaign_id is not None
            and (not isinstance(campaign_id, str) or not campaign_id.strip())
        ):
            return ToolResult(
                content="provide only an optional nonempty campaign_id", is_error=True
            )
        rows = await self._service.list_agents(context.session_id)
        agents = [
            {
                **row.model_dump(mode="json"),
                "can_receive_guidance": row.status in _MESSAGEABLE and row.schedule_id is not None,
            }
            for row in rows
            if row.session_id in {None, context.session_id}
            and (campaign_id is None or row.campaign_id == campaign_id)
        ]
        result = {"agents": agents}
        return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)


class AgentMessageTool:
    effect = "workspace_write"

    def __init__(self, service: OperatorWorkerService) -> None:
        self._service = service

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="agent_message",
            description=(
                "Queue operator context or priorities for a worker in this conversation. "
                "Discover its exact ID with agent_list first. This durable follow-up does not "
                "interrupt or rewrite an active generation, expand authorization, or prove "
                "the worker has read the message. Report the returned delivery state accurately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1, "maxLength": 8192},
                },
                "required": ["agent_id", "message"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        if context.agent_id is not None or context.campaign_id is not None:
            return ToolResult(
                content="operator messaging is available only to foreground chat", is_error=True
            )
        recipient = arguments.get("agent_id")
        message = arguments.get("message")
        if (
            set(arguments) != {"agent_id", "message"}
            or not isinstance(recipient, str)
            or not recipient.strip()
        ):
            return ToolResult(content="provide an exact agent_id and message", is_error=True)
        if not isinstance(message, str) or not message.strip() or len(message) > 8192:
            return ToolResult(content="message must contain 1–8192 characters", is_error=True)
        if not context.call_id:
            return ToolResult(
                content="durable operator delivery requires a tool call ID", is_error=True
            )
        rows = await self._service.list_agents(context.session_id)
        matches = [
            row
            for row in rows
            if recipient in {row.id, row.node_id} and row.session_id in {None, context.session_id}
        ]
        if len(matches) != 1:
            return ToolResult(
                content="worker ID is unavailable or ambiguous; use agent_list",
                is_error=True,
            )
        worker = matches[0]
        if worker.schedule_id is None:
            return ToolResult(content="worker has no durable schedule for delivery", is_error=True)
        node_id = worker.node_id or worker.id
        message_id = str(
            uuid5(NAMESPACE_URL, f"merlin:operator-worker:{context.session_id}:{context.call_id}")
        )
        try:
            replay = await self._service.message_agent_receipt_exists(
                context.session_id,
                node_id,
                message,
                schedule_id=worker.schedule_id,
                message_id=message_id,
            )
            if not replay:
                if worker.status not in _MESSAGEABLE:
                    return ToolResult(
                        content=f"worker is {worker.status}; no new guidance was queued",
                        is_error=True,
                    )
                created = await self._service.message_schedule_agent_once(
                    worker.schedule_id,
                    context.session_id,
                    node_id,
                    message,
                    message_id=message_id,
                    mode=DeliveryMode.FOLLOW_UP,
                    sender_id="operator",
                )
                replay = not created
        except (KeyError, ValueError, RuntimeError):
            return ToolResult(
                content=(
                    "worker guidance delivery could not be confirmed; "
                    "rediscover the recipient before retrying"
                ),
                is_error=True,
            )
        result = {
            "message_id": message_id,
            "agent_id": worker.id,
            "schedule_id": worker.schedule_id,
            "campaign_id": worker.campaign_id,
            "state": "queued",
            "duplicate": replay,
            "delivery_note": (
                "Durably queued; receipt does not establish that the worker has read it."
            ),
        }
        return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)
