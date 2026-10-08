from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentStatus = Literal[
    "queued",
    "ready",
    "leased",
    "running",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "quarantined",
    "cleanup_required",
]
TaskStatus = Literal[
    "queued",
    "ready",
    "leased",
    "running",
    "completed",
    "failed",
    "interrupted",
    "cancelled",
    "quarantined",
    "cleanup_required",
]
ProgressKind = Literal[
    "swarm",
    "schedule",
    "agent",
    "node",
    "attempt",
    "task",
    "lease",
    "retry",
    "replacement",
    "mailbox",
    "directive",
    "message",
    "child",
]


class AgentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AgentSummary(AgentContract):
    id: str
    name: str
    status: AgentStatus
    task_id: str | None = None
    schedule_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    profile_id: str | None = None
    lease_generation: int | None = Field(default=None, ge=1)
    session_id: str | None = None
    campaign_id: str | None = None
    specialist_instance_id: str | None = None
    specialist_display_name: str | None = None
    identity_provenance: str | None = None
    execution_stage: str | None = None
    attempt_owner: str | None = None


class AgentTask(AgentContract):
    id: str
    objective: str
    status: TaskStatus
    agent_id: str | None = None
    result: str | None = None
    technique_ids: tuple[str, ...] = ()


class AgentProgress(AgentContract):
    kind: ProgressKind
    state: str
    text: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    schedule_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    lease_generation: int | None = Field(default=None, ge=1)
    replacement_attempt_id: str | None = None
    retry_count: int | None = Field(default=None, ge=0)
    mailbox_message_id: str | None = None
    directive_id: str | None = None


class LeaseProjection(AgentContract):
    attempt_id: str
    owner_id: str
    generation: int = Field(ge=1)
    expires_at_tick: int = Field(ge=0)


class AttemptProjection(AgentContract):
    attempt_id: str
    node_id: str
    status: AgentStatus
    owner_id: str | None = None
    lease_generation: int = Field(ge=1)
    retry_number: int = Field(default=0, ge=0)
    replaced_attempt_id: str | None = None
    result: str | None = None


class NodeProjection(AgentContract):
    node_id: str
    objective: str
    status: TaskStatus
    profile_id: str | None = None
    technique_ids: tuple[str, ...] = ()
    target_profile_id: str | None = None
    authority_ids: tuple[str, ...] = ()
    budget_summary: str = ""
    tool_names: tuple[str, ...] = ()
    guidance_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    provider_route: str | None = None
    dependency_node_ids: tuple[str, ...] = ()
    attempts_started: int = Field(default=0, ge=0)
    failed_attempts: int = Field(default=0, ge=0)
    lease_reassignments: int = Field(default=0, ge=0)
    last_lease_generation: int = Field(default=0, ge=0)
    lease: LeaseProjection | None = None
    cleanup_required: bool = False


class ScheduleProjection(AgentContract):
    schedule_id: str
    session_id: str
    status: TaskStatus | Literal["paused"]
    topology_digest: str
    event_count: int = Field(default=0, ge=0)
    nodes: tuple[NodeProjection, ...] = ()
    attempts: tuple[AttemptProjection, ...] = ()
    cancelled_reason: str | None = None


class MailboxDeliveryRequest(AgentContract):
    session_id: str
    schedule_id: str | None = None
    recipient_agent_id: str
    message: str = Field(min_length=1, max_length=8192)
    sender_id: str = "operator"
    expected_attempt_id: str | None = None
    expected_lease_generation: int | None = Field(default=None, ge=1)


class MailboxDelivery(AgentContract):
    message_id: str
    schedule_id: str
    recipient_agent_id: str
    state: Literal["queued", "claimed", "delivered", "acknowledged", "cancelled"]
    attempt_id: str | None = None
    lease_generation: int | None = Field(default=None, ge=1)


class RedirectDirective(AgentContract):
    action: Literal["redirect"] = "redirect"
    node_id: str
    guidance: str = Field(min_length=1, max_length=8192)


class HandoffDirective(AgentContract):
    action: Literal["handoff"] = "handoff"
    from_node_id: str
    to_node_id: str
    guidance: str = Field(min_length=1, max_length=8192)


class CancelDirective(AgentContract):
    action: Literal["cancel"] = "cancel"
    node_id: str
    reason: str = Field(min_length=1, max_length=8192)


LeadDirective = Annotated[
    RedirectDirective | HandoffDirective | CancelDirective,
    Field(discriminator="action"),
]


class LeadDecision(AgentContract):
    decision_id: str
    schedule_id: str
    summary: str = Field(min_length=1, max_length=8192)
    evidence_message_ids: tuple[str, ...] = ()
    directives: tuple[LeadDirective, ...] = Field(min_length=1, max_length=32)


class ChildSwarmRequest(AgentContract):
    session_id: str
    parent_schedule_id: str
    parent_node_id: str
    parent_attempt_id: str
    objective: str = Field(min_length=1)
    provider_routes: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    grant_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    remaining_delegation_depth: int = Field(ge=0, le=2)
    max_workers: int = Field(default=1, ge=1, le=3)


class SwarmRequest(AgentContract):
    session_id: str
    objective: str = Field(min_length=1)
    max_workers: int = Field(default=3, ge=1, le=3)
    allow_recursive_delegation: bool = False
    max_delegation_depth: int = Field(default=0, ge=0, le=2)
    specialist_composition: Literal["opinionated", "autonomous"] = "opinionated"
