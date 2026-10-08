import pytest
from merlin_agents import (
    GraphAuthority,
    GraphBounds,
    GraphEdge,
    GraphError,
    GraphNode,
    GraphSpec,
    RetryPolicySpec,
    SchemaSpec,
    SwarmTaskSpec,
    admit_child_graph,
    admit_graph,
    build_swarm_graph,
)

SCHEMAS = (
    SchemaSpec(id="envelope", fields={"payload"}),
    SchemaSpec(id="result", fields={"payload"}),
)


def swarm(*, depth: int = 0) -> GraphSpec:
    tasks = tuple(
        SwarmTaskSpec(
            id=name,
            input_schema="envelope",
            result_schema="result",
            tools={"read"},
            grants={"workspace"},
            max_delegation_depth=depth,
        )
        for name in ("headers", "body")
    )
    return build_swarm_graph(
        "assessment", tasks, SCHEMAS, envelope_schema="envelope", reduced_schema="result"
    )


def authority(*, depth: int = 0) -> GraphAuthority:
    return GraphAuthority(
        authority_id="session:s1",
        tools={"read"},
        grants={"workspace"},
        max_delegation_depth=depth,
    )


def test_builds_cleanup_fan_in_topology_and_stable_digest() -> None:
    spec = swarm()
    admitted = admit_graph(spec, authority(), GraphBounds(max_delegation_depth=0))
    assert [node.kind.value for node in spec.nodes].count("cleanup") == 2
    assert [edge.kind.value for edge in spec.edges].count("fan_in") == 2
    assert admitted.topology_digest == admit_graph(spec, authority()).topology_digest
    reordered = spec.model_copy(
        update={
            "nodes": tuple(reversed(spec.nodes)),
            "edges": tuple(reversed(spec.edges)),
            "schemas": tuple(reversed(spec.schemas)),
        }
    )
    assert admitted.topology_digest == admit_graph(reordered, authority()).topology_digest
    assert admitted.topology_digest.startswith("sha256:")


def test_single_worker_child_shape_has_cleanup_and_deterministic_reducer() -> None:
    spec = build_swarm_graph(
        "child",
        (SwarmTaskSpec(id="only", input_schema="envelope", result_schema="result"),),
        SCHEMAS,
        envelope_schema="envelope",
        reduced_schema="result",
    )
    admitted = admit_graph(spec, GraphAuthority(authority_id="child"), GraphBounds(max_fanout=1))
    assert admitted.spec.nodes[-1].id == "reducer"
    assert [edge.kind.value for edge in admitted.spec.edges].count("fan_in") == 1


def test_rejects_bounds_cycles_dependencies_schemas_and_authority_widening() -> None:
    spec = swarm()
    with pytest.raises(GraphError, match="node or edge"):
        admit_graph(spec, authority(), GraphBounds(max_nodes=2))

    cycle = GraphSpec(
        id="cycle",
        schemas=SCHEMAS,
        nodes=(
            GraphNode(id="a", kind="start", input_schema="envelope", result_schema="envelope"),
            GraphNode(id="b", kind="start", input_schema="envelope", result_schema="envelope"),
        ),
        edges=(
            GraphEdge(id="ab", source="a", target="b", kind="dependency"),
            GraphEdge(id="ba", source="b", target="a", kind="dependency"),
        ),
    )
    with pytest.raises(GraphError, match="cycle"):
        admit_graph(cycle, authority())

    unknown = spec.model_copy(
        update={
            "edges": (
                *spec.edges,
                GraphEdge(id="bad", source="missing", target="start", kind="dependency"),
            )
        }
    )
    with pytest.raises(GraphError, match="unknown node"):
        admit_graph(unknown, authority())

    bad_schema = spec.model_copy(
        update={
            "nodes": (
                spec.nodes[0].model_copy(update={"input_schema": "missing"}),
                *spec.nodes[1:],
            )
        }
    )
    with pytest.raises(GraphError, match="unknown schema"):
        admit_graph(bad_schema, authority())

    with pytest.raises(GraphError, match="authority"):
        admit_graph(spec, authority().model_copy(update={"tools": frozenset()}))


def test_retry_edges_are_bounded_but_not_dependency_cycles() -> None:
    spec = swarm()
    retry = GraphEdge(
        id="retry:headers",
        source="cleanup:headers",
        target="worker:headers",
        kind="retry",
        max_retries=1,
    )
    admitted = admit_graph(spec.model_copy(update={"edges": (*spec.edges, retry)}), authority())
    assert admitted.spec.edges[-1].kind.value == "retry"
    with pytest.raises(GraphError, match="total retry"):
        admit_graph(
            spec.model_copy(
                update={"edges": (*spec.edges, retry.model_copy(update={"max_retries": 3}))}
            ),
            authority(),
        )


def test_typed_retry_policy_replaces_fixed_retry_count() -> None:
    spec = swarm()
    retry = GraphEdge(
        id="retry:headers",
        source="worker:headers",
        target="worker:headers",
        kind="retry",
        retry_policy=RetryPolicySpec(
            failure_kinds={"adapter_timeout", "provider_truncated"},
            max_consecutive_stalls=3,
        ),
    )

    admitted = admit_graph(spec.model_copy(update={"edges": (*spec.edges, retry)}), authority())

    assert admitted.spec.edges[-1].retry_policy == retry.retry_policy
    with pytest.raises(GraphError, match="cannot also set"):
        admit_graph(
            spec.model_copy(
                update={"edges": (*spec.edges, retry.model_copy(update={"max_retries": 1}))}
            ),
            authority(),
        )
    with pytest.raises(GraphError, match="stall bound"):
        admit_graph(
            spec.model_copy(
                update={
                    "edges": (
                        *spec.edges,
                        retry.model_copy(
                            update={
                                "retry_policy": retry.retry_policy.model_copy(
                                    update={"max_consecutive_stalls": 9}
                                )
                            }
                        ),
                    )
                }
            ),
            authority(),
        )


def test_child_graph_must_narrow_authority_and_consume_depth() -> None:
    parent = admit_graph(swarm(depth=1), authority(depth=1), GraphBounds(max_delegation_depth=1))
    child_spec = swarm()
    child_authority = GraphAuthority(
        authority_id="child", tools={"read"}, grants={"workspace"}, max_delegation_depth=0
    )
    child = admit_child_graph(parent, "worker:headers", child_spec, child_authority)
    assert child.authority.max_delegation_depth == 0

    widened = child_authority.model_copy(update={"tools": frozenset({"write"})})
    with pytest.raises(GraphError, match="widens"):
        admit_child_graph(parent, "worker:headers", child_spec, widened)
    with pytest.raises(GraphError, match="exhausted"):
        admit_child_graph(child, "worker:headers", child_spec, child_authority)
