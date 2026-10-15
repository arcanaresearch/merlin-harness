"""Durable operator questions, independent of model prose and client layout."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chat import ChatMessage

ANSWER_PREFIX = "MERLIN_USER_INPUT_RESPONSE\n"


class InputOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class InputQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=2000)
    options: list[InputOption] = Field(default_factory=list, max_length=12)
    multiple: bool = False
    default: list[str] = Field(default_factory=list)
    allow_free_text: bool = True

    @model_validator(mode="after")
    def valid_options(self) -> Self:
        ids = [option.id for option in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("option IDs must be unique within a question")
        if len(set(self.default)) != len(self.default) or set(self.default) - set(ids):
            raise ValueError("defaults must be unique existing option IDs")
        if not self.multiple and len(self.default) > 1:
            raise ValueError("single-select questions accept at most one default")
        if not ids and not self.allow_free_text:
            raise ValueError("a question requires options or free-text input")
        # Product choices have stable defaults regardless of model option order.
        # Other choice questions keep an explicit default or select their first
        # option. A default is only a UI selection, never a submitted answer.
        labels = {
            option.label.casefold().removesuffix(" (default)").removesuffix(" [default]"): option
            for option in self.options
        }
        profiles = {"quickscan", "recommended", "exhaustive", "exploration / learning"}
        composition = {"autonomous", "opinionated"}
        preferred = None
        if not self.multiple and len(labels) > 1:
            if set(labels) <= profiles and "recommended" in labels:
                preferred = "recommended"
            elif set(labels) == composition:
                preferred = "autonomous"
        if preferred is not None:
            self.default = [labels[preferred].id]
            for option in self.options:
                option.label = option.label.removesuffix(" (default)").removesuffix(" [default]")
        elif ids and not self.default:
            self.default = [ids[0]]
        return self


class InputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1)
    questions: list[InputQuestion] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_questions(self) -> Self:
        ids = [question.id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question IDs must be unique")
        return self


class InputAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    option_ids: list[str] = Field(default_factory=list)
    text: str = Field(default="", max_length=10000)


class InputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    answers: dict[str, InputAnswer]

    def validate_for(self, request: InputRequest) -> None:
        if self.request_id != request.request_id:
            raise ValueError("This question is no longer pending; refresh before answering")
        if set(self.answers) != {question.id for question in request.questions}:
            raise ValueError("Answer each question exactly once")
        for question in request.questions:
            answer = self.answers[question.id]
            selected = answer.option_ids
            if len(set(selected)) != len(selected):
                raise ValueError("Duplicate option IDs")
            if set(selected) - {option.id for option in question.options}:
                raise ValueError("Unknown option ID")
            if not question.multiple and len(selected) > 1:
                raise ValueError("Choose one option for this question")
            if answer.text and not question.allow_free_text:
                raise ValueError("Free text is not available for this question")
            if not selected and not answer.text:
                raise ValueError("Select an option or enter an answer")


def pending_input(messages: Sequence[ChatMessage]) -> InputRequest | None:
    pending = None
    for message in messages:
        if message.role == "tool" and getattr(message, "name", None) == "request_user_input":
            try:
                value = json.loads(message.content)
                if value.get("status") == "awaiting_input":
                    pending = InputRequest.model_validate(value["request"])
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        elif message.role == "user":
            # Ordinary language can answer, correct, or cancel the question too.
            pending = None
    return pending


def answer_prompt(request: InputRequest, response: InputResponse) -> str:
    response.validate_for(request)
    return ANSWER_PREFIX + response.model_dump_json()
