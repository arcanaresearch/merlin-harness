import pytest
from merlin_agents.campaign_service import (
    FrontierHypothesis,
    LeadAssessment,
    LeadFollowUp,
    _assessment_evidence_graph,
)
from merlin_agents.evidence_graph import (
    EVIDENCE_GRAPH_VERSION,
    EvidenceEdge,
    EvidenceEdgeKind,
    EvidenceGraph,
    EvidenceGraphError,
    EvidenceNode,
    EvidenceNodeKind,
)
from pydantic import ValidationError


def node(kind: EvidenceNodeKind, name: str) -> EvidenceNode:
    return EvidenceNode.create(
        kind,
        attributes={"name": name, "contract": {"channel": "chat", "revision": 1}},
        evidence_refs=(f"evidence:{name}",),
    )


def test_defines_the_complete_typed_vocabulary() -> None:
    assert {kind.value for kind in EvidenceNodeKind} == {
        "surface",
        "identity",
        "session",
        "object",
        "state",
        "invariant",
        "divergence",
        "attempt",
        "exchange",
        "oracle",
        "validation",
        "finding",
    }
    assert {kind.value for kind in EvidenceEdgeKind} == {
        "observed_on",
        "controls",
        "transitions",
        "violates",
        "premise_for",
        "tested_by",
        "control_for",
        "supported_by",
        "refuted_by",
        "validated_as",
    }


def test_ids_and_serialization_are_content_addressed_and_deterministic() -> None:
    surface = node(EvidenceNodeKind.SURFACE, "message endpoint")
    attempt = node(EvidenceNodeKind.ATTEMPT, "cross-context object access")
    edge = EvidenceEdge.create(surface.id, attempt.id, EvidenceEdgeKind.TESTED_BY)
    graph = EvidenceGraph.create(nodes=(surface, attempt), edges=(edge,))

    same_surface = EvidenceNode.create(
        EvidenceNodeKind.SURFACE,
        attributes={"contract": {"revision": 1, "channel": "chat"}, "name": "message endpoint"},
        evidence_refs=("evidence:message endpoint",),
    )
    reordered = EvidenceGraph.create(nodes=(attempt, same_surface), edges=(edge,))

    assert same_surface.id == surface.id
    assert reordered.id == graph.id
    assert reordered.canonical_json() == graph.canonical_json()
    assert graph.id.startswith("graph:sha256:")
    assert graph.graph_version == EVIDENCE_GRAPH_VERSION


def test_content_changes_change_node_edge_and_graph_ids() -> None:
    surface = node(EvidenceNodeKind.SURFACE, "chat")
    first = node(EvidenceNodeKind.STATE, "unprivileged")
    second = node(EvidenceNodeKind.STATE, "privileged")
    first_edge = EvidenceEdge.create(surface.id, first.id, EvidenceEdgeKind.TRANSITIONS)
    second_edge = EvidenceEdge.create(surface.id, second.id, EvidenceEdgeKind.TRANSITIONS)

    assert first.id != second.id
    assert first_edge.id != second_edge.id
    first_graph = EvidenceGraph.create(nodes=(surface, first), edges=(first_edge,))
    second_graph = EvidenceGraph.create(nodes=(surface, second), edges=(second_edge,))
    assert first_graph.id != second_graph.id


def test_rejects_forged_ids_unknown_references_and_duplicates() -> None:
    surface = node(EvidenceNodeKind.SURFACE, "chat")
    attempt = node(EvidenceNodeKind.ATTEMPT, "probe")
    edge = EvidenceEdge.create(surface.id, attempt.id, EvidenceEdgeKind.TESTED_BY)

    with pytest.raises(ValidationError, match="node id does not match"):
        surface.model_copy(update={"id": "node:sha256:forged"}, deep=True).__class__.model_validate(
            {**surface.model_dump(), "id": "node:sha256:forged"}
        )
    with pytest.raises(ValidationError, match="unknown node"):
        EvidenceGraph.create(nodes=(surface,), edges=(edge,))
    with pytest.raises(ValidationError, match="duplicate node"):
        EvidenceGraph.create(nodes=(surface, surface))
    with pytest.raises(ValidationError, match="duplicate edge"):
        EvidenceGraph.create(nodes=(surface, attempt), edges=(edge, edge))


def test_rejects_non_json_or_ambiguous_canonical_values() -> None:
    with pytest.raises(EvidenceGraphError, match="keys must be strings"):
        EvidenceNode.create(EvidenceNodeKind.OBJECT, attributes={1: "not portable"})  # type: ignore[dict-item]
    with pytest.raises(EvidenceGraphError, match="non-finite"):
        EvidenceNode.create(EvidenceNodeKind.OBJECT, attributes={"score": float("nan")})
    with pytest.raises(EvidenceGraphError, match="duplicate evidence"):
        EvidenceNode.create(
            EvidenceNodeKind.OBJECT,
            evidence_refs=("evidence:1", "evidence:1"),
        )


def test_platform_neutral_causal_chain_represents_a_live_service() -> None:
    identity = EvidenceNode.create(
        EvidenceNodeKind.IDENTITY,
        attributes={"tenant": "tenant-a", "principal": "customer-a"},
    )
    object_node = EvidenceNode.create(
        EvidenceNodeKind.OBJECT,
        attributes={"type": "conversation", "locator": "opaque-object-a"},
    )
    invariant = EvidenceNode.create(
        EvidenceNodeKind.INVARIANT,
        attributes={"assertion": "a principal can read only its own conversation"},
    )
    attempt = EvidenceNode.create(
        EvidenceNodeKind.ATTEMPT,
        attributes={"mutation": "replace opaque object locator"},
    )
    oracle = EvidenceNode.create(
        EvidenceNodeKind.ORACLE,
        attributes={"predicate": "response contains the other principal's sentinel"},
    )
    validation = EvidenceNode.create(
        EvidenceNodeKind.VALIDATION,
        attributes={"outcome": "confirmed", "repeat_count": 2},
    )
    finding = EvidenceNode.create(
        EvidenceNodeKind.FINDING,
        attributes={"class": "object authorization isolation failure"},
    )
    nodes = (identity, object_node, invariant, attempt, oracle, validation, finding)
    edges = (
        EvidenceEdge.create(identity.id, object_node.id, EvidenceEdgeKind.CONTROLS),
        EvidenceEdge.create(invariant.id, attempt.id, EvidenceEdgeKind.TESTED_BY),
        EvidenceEdge.create(attempt.id, oracle.id, EvidenceEdgeKind.PREMISE_FOR),
        EvidenceEdge.create(oracle.id, validation.id, EvidenceEdgeKind.SUPPORTED_BY),
        EvidenceEdge.create(validation.id, finding.id, EvidenceEdgeKind.VALIDATED_AS),
    )

    graph = EvidenceGraph.create(nodes=nodes, edges=edges)

    assert len(graph.nodes) == 7
    assert len(graph.edges) == 5
    assert "game" not in graph.canonical_json()
    assert "level" not in graph.canonical_json()


def test_lead_assessment_seals_hypothesis_and_oracle_causality() -> None:
    assessment = LeadAssessment(
        summary="Recon observed separate identity and session dimensions.",
        coverage_complete=False,
        followups=(
            LeadFollowUp(
                profile_id="experiment-planner",
                objective="Plan a controlled cross-session experiment.",
                source_task_ids=("task:recon",),
                hypothesis=FrontierHypothesis(
                    surface="POST /chat",
                    assertion="session ownership is not enforced",
                    oracle="a foreign session cannot be read",
                    novelty_basis="identity and session are separate request dimensions",
                ),
            ),
        ),
    )

    graph = _assessment_evidence_graph("campaign:1", 2, assessment)

    assert {node.kind for node in graph.nodes} == {
        EvidenceNodeKind.STATE,
        EvidenceNodeKind.DIVERGENCE,
        EvidenceNodeKind.ORACLE,
    }
    assert {edge.kind for edge in graph.edges} == {
        EvidenceEdgeKind.PREMISE_FOR,
        EvidenceEdgeKind.TESTED_BY,
    }
    assert "task:recon" in graph.canonical_json()
