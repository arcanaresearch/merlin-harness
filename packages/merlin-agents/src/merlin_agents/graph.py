from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOPOLOGY_VERSION = "merlin.graph.v1"


class GraphError(ValueError):
    pass


class NodeKind(StrEnum):
    START = "start"
    AGENT = "agent"
    CLEANUP = "cleanup"
    REDUCER = "reducer"


class EdgeKind(StrEnum):
    DEPENDENCY = "dependency"
    ORDER_ONLY = "order_only"
    FAN_OUT = "fan_out"
    CLEANUP = "cleanup"
    FAN_IN = "fan_in"
    RETRY = "retry"


class GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaSpec(GraphModel):
    id: str
    fields: frozenset[str] = frozenset()


class GraphAuthority(GraphModel):
    authority_id: str
    tools: frozenset[str] = frozenset()
    grants: frozenset[str] = frozenset()
    max_delegation_depth: int = Field(default=0, ge=0)


class AgentBinding(GraphModel):
    agent_id: str
    tools: frozenset[str] = frozenset()
    grants: frozenset[str] = frozenset()
    max_delegation_depth: int = Field(default=0, ge=0)


class GraphNode(GraphModel):
    id: str
    kind: NodeKind
    input_schema: str
    result_schema: str
    agent: AgentBinding | None = None


class RetryPolicySpec(GraphModel):
    """Typed, progress-bounded replacement policy for one graph node."""

    failure_kinds: frozenset[
        Literal[
            "provider_truncated",
            "provider_transient",
            "adapter_timeout",
            "pointer_repair",
            "transport_transient",
            "target_adapter_unhealthy",
            "worker_interrupted",
            "missing_target_effect",
            "result_schema_invalid",
        ]
    ] = Field(min_length=1)
    max_consecutive_stalls: int = Field(default=3, ge=1)
    # Per-kind stall overrides for fail-fast worker rules; a kind at zero goes
    # terminal on its first occurrence instead of burning replacement workers.
    failure_kind_stall_limits: Mapping[str, int] = Field(default_factory=dict)

    @field_validator("failure_kind_stall_limits")
    @classmethod
    def _stall_limits_are_bounded(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(limit < 0 for limit in value.values()):
            raise ValueError("per-kind retry stall limits cannot be negative")
        return value


class GraphEdge(GraphModel):
    id: str
    source: str
    target: str
    kind: EdgeKind
    permitted_fields: frozenset[str] = frozenset()
    retry_policy: RetryPolicySpec | None = None
    # Read compatibility for graphs admitted before typed retry policies.
    max_retries: int = Field(default=0, ge=0)


class GraphSpec(GraphModel):
    id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    schemas: tuple[SchemaSpec, ...]
    topology_version: Literal["merlin.graph.v1"] = TOPOLOGY_VERSION


class GraphBounds(GraphModel):
    max_nodes: int = Field(default=16, ge=1)
    max_edges: int = Field(default=24, ge=1)
    max_fanout: int = Field(default=3, ge=1)
    max_retry_edges: int = Field(default=2, ge=0)
    max_total_retries: int = Field(default=2, ge=0)
    max_retry_stalls_per_edge: int = Field(default=8, ge=1)
    max_delegation_depth: int = Field(default=0, ge=0)


class AdmittedGraph(GraphModel):
    spec: GraphSpec
    authority: GraphAuthority
    topology_digest: str


class SwarmTaskSpec(GraphModel):
    id: str
    input_schema: str
    result_schema: str
    tools: frozenset[str] = frozenset()
    grants: frozenset[str] = frozenset()
    max_delegation_depth: int = Field(default=0, ge=0)


def canonical_digest(model: BaseModel) -> str:
    value = model.model_dump(mode="python")
    if isinstance(model, GraphSpec):
        for collection in ("nodes", "edges", "schemas"):
            value[collection] = sorted(value[collection], key=lambda item: item["id"])
    encoded = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def admit_graph(
    spec: GraphSpec, authority: GraphAuthority, bounds: GraphBounds | None = None
) -> AdmittedGraph:
    _validate(spec, authority, bounds or GraphBounds())
    return AdmittedGraph(spec=spec, authority=authority, topology_digest=canonical_digest(spec))


def narrow_child_authority(
    parent: AdmittedGraph, delegating_node_id: str, requested: GraphAuthority
) -> GraphAuthority:
    node = next((item for item in parent.spec.nodes if item.id == delegating_node_id), None)
    if node is None or node.agent is None:
        raise GraphError("delegating node is not an admitted agent")
    available_depth = min(parent.authority.max_delegation_depth, node.agent.max_delegation_depth)
    if available_depth < 1:
        raise GraphError("delegation depth is exhausted")
    if not requested.tools <= node.agent.tools or not requested.grants <= node.agent.grants:
        raise GraphError("child authority widens the delegating agent")
    if requested.max_delegation_depth > available_depth - 1:
        raise GraphError("child delegation depth is not narrower")
    return requested


def admit_child_graph(
    parent: AdmittedGraph,
    delegating_node_id: str,
    spec: GraphSpec,
    requested_authority: GraphAuthority,
    bounds: GraphBounds | None = None,
) -> AdmittedGraph:
    authority = narrow_child_authority(parent, delegating_node_id, requested_authority)
    return admit_graph(spec, authority, bounds)


def build_swarm_graph(
    graph_id: str,
    tasks: tuple[SwarmTaskSpec, ...],
    schemas: tuple[SchemaSpec, ...],
    *,
    envelope_schema: str,
    reduced_schema: str,
) -> GraphSpec:
    if not tasks:
        raise GraphError("a graph requires at least one task")
    nodes = [
        GraphNode(
            id="start", kind="start", input_schema=envelope_schema, result_schema=envelope_schema
        )
    ]
    edges: list[GraphEdge] = []
    for task in tasks:
        worker_id, cleanup_id = f"worker:{task.id}", f"cleanup:{task.id}"
        binding = AgentBinding(
            agent_id=f"agent:{task.id}",
            tools=task.tools,
            grants=task.grants,
            max_delegation_depth=task.max_delegation_depth,
        )
        nodes.extend(
            (
                GraphNode(
                    id=worker_id,
                    kind="agent",
                    input_schema=task.input_schema,
                    result_schema=task.result_schema,
                    agent=binding,
                ),
                GraphNode(
                    id=cleanup_id,
                    kind="cleanup",
                    input_schema=task.result_schema,
                    result_schema=task.result_schema,
                ),
            )
        )
        edges.extend(
            (
                GraphEdge(id=f"start:{task.id}", source="start", target=worker_id, kind="fan_out"),
                GraphEdge(
                    id=f"cleanup:{task.id}",
                    source=worker_id,
                    target=cleanup_id,
                    kind="cleanup",
                ),
                GraphEdge(
                    id=f"reduce:{task.id}",
                    source=cleanup_id,
                    target="reducer",
                    kind="fan_in",
                ),
            )
        )
    nodes.append(
        GraphNode(
            id="reducer",
            kind="reducer",
            input_schema=reduced_schema,
            result_schema=reduced_schema,
        )
    )
    return GraphSpec(id=graph_id, nodes=tuple(nodes), edges=tuple(edges), schemas=schemas)


def _validate(spec: GraphSpec, authority: GraphAuthority, bounds: GraphBounds) -> None:
    if len(spec.nodes) > bounds.max_nodes or len(spec.edges) > bounds.max_edges:
        raise GraphError("graph exceeds node or edge bounds")
    nodes = _unique(spec.nodes, "node")
    edges = _unique(spec.edges, "edge")
    schemas = _unique(spec.schemas, "schema")
    if not nodes:
        raise GraphError("graph has no nodes")
    retries = [edge for edge in edges.values() if edge.kind is EdgeKind.RETRY]
    if len(retries) > bounds.max_retry_edges:
        raise GraphError("graph exceeds retry-edge bound")
    if sum(edge.max_retries for edge in retries if edge.retry_policy is None) > (
        bounds.max_total_retries
    ):
        raise GraphError("graph exceeds total retry bound")
    if any(
        edge.retry_policy is not None
        and edge.retry_policy.max_consecutive_stalls > bounds.max_retry_stalls_per_edge
        for edge in retries
    ):
        raise GraphError("graph exceeds retry stall bound")
    incoming: dict[str, int] = defaultdict(int)
    outgoing: dict[str, int] = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges.values():
        if edge.source not in nodes or edge.target not in nodes:
            raise GraphError("edge references an unknown node")
        if edge.kind is EdgeKind.RETRY:
            if edge.retry_policy is None and edge.max_retries < 1:
                raise GraphError("retry edge must define a typed policy or retry count")
            if edge.retry_policy is not None and edge.max_retries:
                raise GraphError("typed retry policy cannot also set a fixed retry count")
            continue
        if edge.max_retries or edge.retry_policy is not None:
            raise GraphError("only retry edges may define retry behavior")
        incoming[edge.target] += 1
        outgoing[edge.source] += 1
        adjacency[edge.source].append(edge.target)
        if edge.kind is EdgeKind.FAN_OUT and nodes[edge.source].kind is not NodeKind.START:
            raise GraphError("fan-out edge must originate at the start node")
        if edge.kind is EdgeKind.CLEANUP and nodes[edge.target].kind is not NodeKind.CLEANUP:
            raise GraphError("cleanup edge must target a cleanup node")
        if edge.kind is EdgeKind.FAN_IN and nodes[edge.target].kind is not NodeKind.REDUCER:
            raise GraphError("fan-in edge must target a reducer")
        source_schema = schemas.get(nodes[edge.source].result_schema)
        target_schema = schemas.get(nodes[edge.target].input_schema)
        if source_schema is None or target_schema is None:
            raise GraphError("edge references an unknown schema")
        if not edge.permitted_fields <= (source_schema.fields & target_schema.fields):
            raise GraphError("edge permits fields outside its schemas")
    if max(outgoing.values(), default=0) > bounds.max_fanout:
        raise GraphError("graph exceeds fan-out bound")
    for node in nodes.values():
        if node.input_schema not in schemas or node.result_schema not in schemas:
            raise GraphError("node references an unknown schema")
        if (node.kind is NodeKind.AGENT) != (node.agent is not None):
            raise GraphError("only agent nodes may carry agent bindings")
        if node.agent is not None:
            if not node.agent.tools <= authority.tools or not node.agent.grants <= authority.grants:
                raise GraphError("agent authority exceeds graph authority")
            if node.agent.max_delegation_depth > min(
                authority.max_delegation_depth, bounds.max_delegation_depth
            ):
                raise GraphError("agent delegation depth exceeds authority")
        if node.kind is NodeKind.CLEANUP and (incoming[node.id] != 1 or outgoing[node.id] != 1):
            raise GraphError("cleanup node must have one input and one output")
        if node.kind is NodeKind.REDUCER and incoming[node.id] < 1:
            raise GraphError("reducer must fan in at least one branch")
        if node.kind is not NodeKind.START and incoming[node.id] == 0:
            raise GraphError("non-root node has no dependency")
    _reject_cycles(nodes, adjacency, incoming)
    starts = [node for node in nodes.values() if node.kind is NodeKind.START]
    if len(starts) != 1 or incoming[starts[0].id]:
        raise GraphError("graph must have one dependency root")
    if _reachable(starts[0].id, adjacency) != set(nodes):
        raise GraphError("dependency graph contains unreachable nodes")


def _unique(items: tuple[GraphModel, ...], label: str) -> dict[str, GraphModel]:
    result = {}
    for item in items:
        if item.id in result:
            raise GraphError(f"duplicate {label} id: {item.id}")
        result[item.id] = item
    return result


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _reject_cycles(nodes: dict, adjacency: dict, incoming: dict) -> None:
    degrees = {node_id: incoming[node_id] for node_id in nodes}
    ready = deque(node_id for node_id, degree in degrees.items() if degree == 0)
    visited = 0
    while ready:
        node_id = ready.popleft()
        visited += 1
        for target in adjacency[node_id]:
            degrees[target] -= 1
            if degrees[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        raise GraphError("dependency graph contains a cycle")


def _reachable(root: str, adjacency: dict[str, list[str]]) -> set[str]:
    found, pending = set(), [root]
    while pending:
        node_id = pending.pop()
        if node_id in found:
            continue
        found.add(node_id)
        pending.extend(adjacency[node_id])
    return found
