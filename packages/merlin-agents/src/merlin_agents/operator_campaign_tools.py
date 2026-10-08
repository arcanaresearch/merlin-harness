"""Session-owned foreground delivery of operator guidance to Campaign Lead."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from merlin_contracts import ToolContext, ToolResult, ToolSpec

if TYPE_CHECKING:
    from .campaign_service import CampaignService

ACTIVE_CAMPAIGN_STATUSES = frozenset({"prepared", "executing", "paused"})


class CampaignMessageTool:
    effect = "workspace_write"

    def __init__(self, service: CampaignService) -> None:
        self._service = service

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="campaign_message",
            description=(
                "Forward the operator's context or priorities to an AI Campaign Lead. "
                "Omit campaign_id only when one active AI campaign belongs to this session; "
                "otherwise use assessment_list and ask which campaign. Delivery is durably "
                "queued and triggers a Lead assessment during the active wave. The Lead can "
                "redirect workers or finish running work and export a partial report. A queued "
                "receipt is not proof of action and does not cancel an in-flight request. "
                "Guidance does not authorize new targets or permissions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            if context.agent_id is not None or context.campaign_id is not None:
                raise ValueError("campaign_message is an operator foreground tool")
            message = arguments.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("message must be a non-empty string")
            campaign_id = arguments.get("campaign_id")
            if campaign_id is not None and (
                not isinstance(campaign_id, str) or not campaign_id.strip()
            ):
                raise ValueError("campaign_id must be a non-empty string")
            if not context.call_id:
                raise ValueError("campaign_message requires a durable tool call identity")
            if campaign_id is None:
                active = [
                    row
                    for row in await self._service.list(context.session_id)
                    if row.get("status") in ACTIVE_CAMPAIGN_STATUSES
                ]
                if not active:
                    raise ValueError("no active AI campaign in this session")
                if len(active) != 1:
                    candidates = [str(row["campaign_id"]) for row in active]
                    return ToolResult(
                        content=json.dumps(
                            {
                                "error": "multiple active AI campaigns; ask the operator which one",
                                "campaign_ids": candidates,
                            },
                            sort_keys=True,
                        ),
                        is_error=True,
                        metadata={"delivery_status": "not_queued", "campaign_ids": candidates},
                    )
                campaign_id = str(active[0]["campaign_id"])
            receipt = await self._service.guide(
                context.session_id, campaign_id, message, request_id=context.call_id
            )
            result = {**receipt, "delivery_status": "queued", "recipient": "campaign_lead"}
            return ToolResult(content=json.dumps(result, sort_keys=True), metadata=result)
        except (KeyError, ValueError) as error:
            result = {"delivery_status": "not_queued", "error": str(error)}
            return ToolResult(
                content=json.dumps(result, sort_keys=True), is_error=True, metadata=result
            )
