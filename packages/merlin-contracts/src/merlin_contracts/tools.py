from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

LEGACY_TARGET_EXCHANGE_TOOL_IDS = frozenset(
    {"target_chat", "target_request", "browser_chat_exchange"}
)


class ToolInteractionRole(StrEnum):
    """Semantic role of a tool interaction, independent of its permission effect."""

    AUXILIARY = "auxiliary"
    TARGET_EXCHANGE = "target_exchange"


class TargetDeliveryClass(StrEnum):
    """Target-side channel used for an authenticated exchange."""

    DIRECT_USER_CHANNEL = "direct-user-channel"
    INTER_AGENT_MESSAGE = "inter-agent-message"


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, object]
    interaction_role: ToolInteractionRole = ToolInteractionRole.AUXILIARY
    target_delivery: TargetDeliveryClass | None = None
    target_delivery_from_reviewed_adapter: bool = False


class ToolContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    workspace_root: str
    call_id: str | None = None
    agent_id: str | None = None
    attempt_id: str | None = None
    technique_attempt_id: str | None = None
    campaign_id: str | None = None
    permission_grant_digest: str | None = None
    target_id: str | None = None
    compiled_adapter: str | None = None
    compiled_adapter_digest: str | None = None
    expected_target_tool: str | None = None
    expected_target_arguments_digest: str | None = None
    expected_target_sequence: tuple[tuple[str, str], ...] = ()
    target_effects_blocked: bool = False
    effect_attempt_lineage: tuple[str, ...] = ()
    transform_bindings: tuple[tuple[str, str, str], ...] = ()
    planned_language: str = "und"
    requested_language: str = "und"
    requested_transform_id: str = "baseline"
    objective_phase_gate_id: str | None = None
    objective_phase_gate_recipe_digest: str | None = None
    objective_phase_manifest_digest: str | None = None
    objective_phase_checkpoint_intent: Literal["capture", "restore"] | None = None
    objective_phase_checkpoint_reference: dict[str, object] | None = None
    objective_phase_gate_contract: dict[str, object] | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    is_error: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class Tool(Protocol):
    def describe(self) -> ToolSpec: ...

    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult: ...
