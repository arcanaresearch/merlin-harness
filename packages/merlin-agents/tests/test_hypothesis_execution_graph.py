import json

from merlin_agents.campaign_service import _hypothesis_execution_evidence_graph
from merlin_agents.evidence_graph import EvidenceEdgeKind, EvidenceNodeKind
from merlin_contracts import AgentTask


def hypothesis_task(*, result: str = "worker prose is not evidence") -> AgentTask:
    contract = {
        "id": "hypothesis:session-object-isolation",
        "surface": "POST /messages",
        "assertion": "an object locator is not bound to the authenticated principal",
        "oracle": "a foreign sentinel is returned",
        "novelty_basis": "identity and object locator vary independently",
        "experiment": {
            "treatment": "replace the locator",
            "control": "retain the original locator",
        },
        "source_task_ids": ["recon-1"],
        "source_receipts": [
            ["recon-1", "sha256:source", "exchange:recon-1"],
        ],
    }
    return AgentTask(
        id="campaign/wave-2/researcher-0",
        objective="Bounded experiment\n\nSealed hypothesis contract: "
        + json.dumps(contract, sort_keys=True),
        status="completed",
        result=result,
        technique_ids=("hypothesis:session-object-isolation",),
    )


def projection(graph) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    value = json.loads(graph.canonical_json())
    return value["nodes"], value["edges"]


def test_execution_graph_links_contract_control_receipts_oracle_and_validation() -> None:
    task = hypothesis_task()
    finding = {
        "id": "finding-1",
        "creator": task.id,
        "technique_id": "hypothesis:session-object-isolation",
        "status": "confirmed",
        "validation": "validated",
        "evidence_references": ("exchange:treatment",),
        "witnesses": (("validator:campaign-1:receipt-1", "sha256:witness", "validator"),),
    }

    graph = _hypothesis_execution_evidence_graph(
        task,
        {
            "digest": "sha256:execution",
            "reference": "receipt:task-1",
            "target_call_ids": ("exchange:treatment", "exchange:control"),
        },
        {"hypothesis:session-object-isolation": "confirmed"},
        schedule_id="schedule-2",
        findings=(finding,),
    )
    nodes, edges = projection(graph)

    kinds = {node["kind"] for node in nodes}
    edge_kinds = {edge["kind"] for edge in edges}
    assert {
        EvidenceNodeKind.SURFACE,
        EvidenceNodeKind.STATE,
        EvidenceNodeKind.DIVERGENCE,
        EvidenceNodeKind.ATTEMPT,
        EvidenceNodeKind.EXCHANGE,
        EvidenceNodeKind.ORACLE,
        EvidenceNodeKind.FINDING,
        EvidenceNodeKind.VALIDATION,
    } <= kinds
    assert {
        EvidenceEdgeKind.OBSERVED_ON,
        EvidenceEdgeKind.PREMISE_FOR,
        EvidenceEdgeKind.TESTED_BY,
        EvidenceEdgeKind.CONTROL_FOR,
        EvidenceEdgeKind.SUPPORTED_BY,
        EvidenceEdgeKind.VALIDATED_AS,
    } <= edge_kinds
    outcome = next(
        node
        for node in nodes
        if node["kind"] == "validation" and node["attributes"].get("hypothesis_id")
    )
    assert outcome["attributes"]["outcome"] == "confirmed"
    serialized = graph.canonical_json()
    assert "worker prose is not evidence" not in serialized
    assert "exchange:treatment" in serialized
    assert "exchange:control" in serialized


def test_execution_graph_degrades_to_unknown_without_validated_finding() -> None:
    task = hypothesis_task(result="I definitely succeeded")

    graph = _hypothesis_execution_evidence_graph(
        task,
        {"digest": "sha256:execution", "target_call_ids": ("exchange:aggregate",)},
        {"hypothesis:session-object-isolation": "confirmed"},
        schedule_id="schedule-2",
    )
    nodes, _edges = projection(graph)

    outcome = next(
        node
        for node in nodes
        if node["kind"] == "validation" and node["attributes"].get("hypothesis_id")
    )
    assert outcome["attributes"] == {
        "authority": "receipt_or_durable_finding",
        "hypothesis_id": "hypothesis:session-object-isolation",
        "outcome": "unknown",
        "raw_receipt_outcome": "confirmed",
    }
    assert "I definitely succeeded" not in graph.canonical_json()


def test_execution_graph_is_deterministic_across_receipt_order() -> None:
    task = hypothesis_task()
    first = _hypothesis_execution_evidence_graph(
        task,
        {
            "digest": "sha256:execution",
            "reference": "receipt:task-1",
            "target_call_ids": ("exchange:b", "exchange:a"),
        },
        {"hypothesis:session-object-isolation": "unclear"},
        schedule_id="schedule-2",
    )
    second = _hypothesis_execution_evidence_graph(
        task,
        {
            "digest": "sha256:execution",
            "reference": "receipt:task-1",
            "target_call_ids": ("exchange:a", "exchange:b"),
        },
        {"hypothesis:session-object-isolation": "unclear"},
        schedule_id="schedule-2",
    )

    assert first.id == second.id
    assert first.canonical_json() == second.canonical_json()
