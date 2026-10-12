"""Foreground structured-question tool; requests persist as normal tool receipts."""

import json
from collections.abc import Mapping
from typing import Any

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_contracts.user_input import InputRequest


class RequestUserInputTool:
    effect = "workspace_read"

    def describe(self) -> ToolSpec:
        schema = InputRequest.model_json_schema()
        schema["properties"].pop("request_id")
        schema["required"].remove("request_id")
        return ToolSpec(
            name="request_user_input",
            description=(
                "Ask the operator a structured question whenever a choice or missing input "
                "is needed, including scan profiles, agent composition, authentication paths "
                "and ambiguous targets. Use stable question/option IDs, clear labels and "
                "descriptions. Do not output numbered choices as prose. Every choice question "
                "must have a default. Put the default first and provide its option ID in default. "
                "Campaign defaults are Quickscan and Autonomous, not Recommended or Opinionated. "
                "Defaults never submit automatically. Allow free text when "
                "useful. Never request passwords, API keys or verification codes here; use "
                "secure setup or browser takeover. The client renders the question and pauses "
                "this foreground turn. The operator's response arrives in the durable "
                "conversation with MERLIN_USER_INPUT_RESPONSE and exact selected IDs, or "
                "as ordinary language. Continue from retained work after the answer; do not "
                "repeat reconnaissance. A choice does not approve execution or expand scope."
            ),
            input_schema=schema,
        )

    async def invoke(self, context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            if context.agent_id is not None or context.campaign_id is not None:
                raise ValueError("questions are available only to the foreground model")
            if not context.call_id or "request_id" in arguments:
                raise ValueError("request identity must come from the durable tool call")
            request = InputRequest(
                request_id=f"{context.session_id}:{context.call_id}", **dict(arguments)
            )
            payload = {"status": "awaiting_input", "request": request.model_dump()}
            return ToolResult(content=json.dumps(payload), metadata=payload)
        except (TypeError, ValueError) as error:
            return ToolResult(content=str(error), is_error=True)
