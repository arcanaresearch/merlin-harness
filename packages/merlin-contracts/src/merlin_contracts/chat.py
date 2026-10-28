from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageAttachment(ContractModel):
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    path: str = Field(min_length=1, max_length=4_096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(gt=0, le=5_242_880)

    @model_validator(mode="after")
    def require_absolute_path(self) -> "ImageAttachment":
        if not self.path.startswith("/"):
            raise ValueError("image attachment path must be absolute")
        return self


class ChatMessage(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    reasoning_content: str | None = None
    image_attachments: list[ImageAttachment] = Field(default_factory=list, max_length=4)


class ChatRequest(ContractModel):
    model: str
    messages: list[ChatMessage]
    tools: list[dict[str, object]] = Field(default_factory=list)
    required_tool_name: str | None = Field(default=None, min_length=1)
    temperature: float | None = None
    reasoning: Literal["enabled", "disabled"] | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    budget_id: str | None = None
    workload: (
        Literal[
            "lead",
            "worker",
            "recon",
            "validator",
            "planner",
            "judge",
            "assessment_attacker",
            "assessment_target",
            "structured_generation",
            "automation",
            "arm_generation",
            "phase_gate",
            "kernel",
            "provider_evaluation",
        ]
        | None
    ) = None
    reasoning_effort: Literal["default", "minimal", "low", "medium", "high", "xhigh"] | None = None
    reasoning_control: Literal["required"] | None = None

    @model_validator(mode="after")
    def require_advertised_tool(self) -> "ChatRequest":
        if self.reasoning_control == "required" and self.reasoning != "disabled":
            raise ValueError("required reasoning control requires reasoning='disabled'")
        if self.required_tool_name is None:
            return self
        advertised = {
            str(function["name"])
            for tool in self.tools
            if isinstance((function := tool.get("function")), dict)
            and isinstance(function.get("name"), str)
        }
        if self.required_tool_name not in advertised:
            raise ValueError("required tool must be present in tools")
        return self


class TextDelta(ContractModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(ContractModel):
    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class ModelUsage(ContractModel):
    """Per-inference usage record with Pirateboys telemetry semantics.

    ``input_tokens`` is the provider-reported total input (uncached + cache-read
    + cache-creation); the ``uncached_input_tokens`` / ``cache_read_input_tokens``
    / ``cache_creation_input_tokens`` fields break it down. Provider-reported
    usage carries ``usage_source="provider"``; a locally estimated fallback is
    only ever recorded as ``usage_source="estimated"`` and never merged into
    provider-reported figures. ``usage_complete=False`` marks interrupted or
    proxy-mediated calls that lack authoritative usage.
    """

    type: Literal["usage"] = "usage"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    uncached_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_hit: bool = False
    cache_created: bool = False
    cache_miss: bool = False
    usage_complete: bool = True
    usage_source: Literal["provider", "estimated"] = "provider"
    request_id: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=1, ge=1)


class ModelWarning(ContractModel):
    type: Literal["warning"] = "warning"
    code: str
    message: str


class ToolCall(ContractModel):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: dict[str, object]


class DynamicToolOutput(ContractModel):
    content: str
    success: bool = True


class ModelCompleted(ContractModel):
    type: Literal["completed"] = "completed"
    finish_reason: str | None = None
    disposition: Literal["completed", "refused", "truncated"] = "completed"


class ProviderFailureKind(StrEnum):
    UNKNOWN = "unknown"
    TRANSIENT = "transient"
    TRUNCATED = "truncated"
    FORMATTING = "formatting"
    AUTHORIZATION = "authorization"
    PERMISSION = "permission"
    QUOTA = "quota"
    BUDGET = "budget"
    CONFIGURATION = "configuration"


class ModelError(ContractModel):
    type: Literal["error"] = "error"
    message: str
    retryable: bool = False
    code: str = "provider_error"
    retry_after_seconds: float | None = Field(default=None, ge=0)
    partial: bool = False
    tool_name: str | None = None
    failure_kind: ProviderFailureKind = ProviderFailureKind.UNKNOWN


ModelEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCall | ModelUsage | ModelWarning | ModelCompleted | ModelError,
    Field(discriminator="type"),
]


class Provider(Protocol):
    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]: ...


DynamicToolHandler = Callable[[ToolCall], Awaitable[DynamicToolOutput]]


class DynamicToolProvider(Protocol):
    def stream_with_tools(
        self, request: ChatRequest, handler: DynamicToolHandler
    ) -> AsyncIterator[ModelEvent]: ...
