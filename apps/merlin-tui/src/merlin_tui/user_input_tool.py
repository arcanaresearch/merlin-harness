"""Foreground structured-question tool; requests persist as normal tool receipts."""

import json
from collections.abc import Mapping
from typing import Any

from merlin_contracts import ToolContext, ToolResult, ToolSpec
from merlin_contracts.user_input import InputQuestion, InputRequest


def _choice_names(question: InputQuestion) -> set[str]:
    return {
        value.casefold().strip()
        for option in question.options
        for value in (option.id, option.label)
    }


def _is_campaign_profile_question(question: InputQuestion) -> bool:
    names = _choice_names(question)
    return {"quickscan", "recommended", "exhaustive"} <= names and any(
        name in names for name in {"exploration", "exploration / learning", "learning"}
    )


def _is_composition_question(question: InputQuestion) -> bool:
    names = _choice_names(question)
    return {"autonomous", "opinionated"} <= names


def _is_campaign_execution_approval_question(question: InputQuestion) -> bool:
    """Reject approval prompts owned by the client's sealed-review control."""
    subject = " ".join(
        (
            question.id,
            question.question,
            *(option.id for option in question.options),
            *(option.label for option in question.options),
        )
    ).casefold()
    return any(term in subject for term in ("campaign", "manifest", "assessment")) and any(
        term in subject for term in ("approve", "execute", "start campaign")
    )


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
                "Campaign defaults are Recommended and Autonomous, not Quickscan or Opinionated. "
                "Defaults never submit automatically. Allow free text when "
                "useful. Never request passwords, API keys or verification codes here; use "
                "secure setup or browser takeover. The client renders the question and pauses "
                "this foreground turn. The operator's response arrives in the durable "
                "conversation with MERLIN_USER_INPUT_RESPONSE and exact selected IDs, or "
                "as ordinary language. Continue from retained work after the answer; do not "
                "repeat reconnaissance. A choice does not approve execution or expand scope. "
                "Never ask for approval of a prepared campaign or manifest with this tool. "
                "assessment_prepare makes the client render the sealed review and approval "
                "control; after it succeeds, end the turn without calling request_user_input "
                "or assessment_execute."
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
            if any(
                _is_campaign_execution_approval_question(question) for question in request.questions
            ):
                raise ValueError(
                    "Campaign execution approval belongs to the Merlin client's sealed-review "
                    "control. The review is already displayed there; stop this turn without "
                    "calling request_user_input or assessment_execute."
                )
            has_profile = any(
                _is_campaign_profile_question(question) for question in request.questions
            )
            has_composition = any(
                _is_composition_question(question) for question in request.questions
            )
            if has_profile and not has_composition:
                if len(request.questions) >= 3:
                    raise ValueError(
                        "campaign profile selection must include agent composition in the same "
                        "request"
                    )
                question_id = "specialist_composition"
                existing_ids = {question.id for question in request.questions}
                suffix = 2
                while question_id in existing_ids:
                    question_id = f"specialist_composition_{suffix}"
                    suffix += 1
                request = InputRequest.model_validate(
                    {
                        **request.model_dump(),
                        "questions": [
                            *(question.model_dump() for question in request.questions),
                            {
                                "id": question_id,
                                "question": "How should Merlin compose the specialist team?",
                                "options": [
                                    {
                                        "id": "autonomous",
                                        "label": "Autonomous",
                                        "description": (
                                            "Let the Campaign Lead compose a target-specific team."
                                        ),
                                    },
                                    {
                                        "id": "opinionated",
                                        "label": "Opinionated",
                                        "description": (
                                            "Use Merlin's reviewed specialist template."
                                        ),
                                    },
                                ],
                                "default": ["autonomous"],
                                "allow_free_text": True,
                            },
                        ],
                    }
                )
            payload = {"status": "awaiting_input", "request": request.model_dump()}
            return ToolResult(content=json.dumps(payload), metadata=payload)
        except (TypeError, ValueError) as error:
            return ToolResult(content=str(error), is_error=True)
