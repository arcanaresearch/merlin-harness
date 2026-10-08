from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EVIDENCE_GRAPH_VERSION = "merlin.evidence-graph.v1"


class EvidenceGraphError(ValueError):
    """Raised when evidence graph content is invalid or internally inconsistent."""


class EvidenceNodeKind(StrEnum):
    SURFACE = "surface"
    IDENTITY = "identity"
    SESSION = "session"
    OBJECT = "object"
    STATE = "state"
    INVARIANT = "invariant"
    DIVERGENCE = "divergence"
    ATTEMPT = "attempt"
    EXCHANGE = "exchange"
    ORACLE = "oracle"
    VALIDATION = "validation"
    FINDING = "finding"


class EvidenceEdgeKind(StrEnum):
    OBSERVED_ON = "observed_on"
    CONTROLS = "controls"
    TRANSITIONS = "transitions"
    VIOLATES = "violates"
    PREMISE_FOR = "premise_for"
    TESTED_BY = "tested_by"
    CONTROL_FOR = "control_for"
    SUPPORTED_BY = "supported_by"
    REFUTED_BY = "refuted_by"
    VALIDATED_AS = "validated_as"


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceNode(_EvidenceModel):
    id: str
    kind: EvidenceNodeKind
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        kind: EvidenceNodeKind,
        *,
        attributes: dict[str, Any] | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> Self:
        payload = _node_payload(kind, attributes or {}, evidence_refs)
        return cls(id=_content_id("node", payload), **payload)

    @model_validator(mode="after")
    def _validate_content_id(self) -> Self:
        payload = _node_payload(self.kind, self.attributes, self.evidence_refs)
        if self.id != _content_id("node", payload):
            raise EvidenceGraphError("node id does not match its canonical content digest")
        return self


class EvidenceEdge(_EvidenceModel):
    id: str
    source: str
    target: str
    kind: EvidenceEdgeKind
    attributes: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        source: str,
        target: str,
        kind: EvidenceEdgeKind,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> Self:
        payload = _edge_payload(source, target, kind, attributes or {})
        return cls(id=_content_id("edge", payload), **payload)

    @model_validator(mode="after")
    def _validate_content_id(self) -> Self:
        payload = _edge_payload(self.source, self.target, self.kind, self.attributes)
        if self.id != _content_id("edge", payload):
            raise EvidenceGraphError("edge id does not match its canonical content digest")
        return self


class EvidenceGraph(_EvidenceModel):
    id: str
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...] = ()
    graph_version: str = EVIDENCE_GRAPH_VERSION

    @classmethod
    def create(
        cls,
        *,
        nodes: tuple[EvidenceNode, ...],
        edges: tuple[EvidenceEdge, ...] = (),
    ) -> Self:
        payload = _graph_payload(nodes, edges, EVIDENCE_GRAPH_VERSION)
        return cls(id=_content_id("graph", payload), **payload)

    @model_validator(mode="after")
    def _validate_graph(self) -> Self:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise EvidenceGraphError("graph contains duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise EvidenceGraphError("graph contains duplicate edge ids")
        known_nodes = set(node_ids)
        for edge in self.edges:
            if edge.source not in known_nodes or edge.target not in known_nodes:
                raise EvidenceGraphError("edge references an unknown node")
        payload = _graph_payload(self.nodes, self.edges, self.graph_version)
        if self.id != _content_id("graph", payload):
            raise EvidenceGraphError("graph id does not match its canonical content digest")
        return self

    def canonical_json(self) -> str:
        payload = _graph_payload(self.nodes, self.edges, self.graph_version)
        return _canonical_json({"id": self.id, **payload})


def _node_payload(
    kind: EvidenceNodeKind,
    attributes: dict[str, Any],
    evidence_refs: tuple[str, ...],
) -> dict[str, Any]:
    if len(evidence_refs) != len(set(evidence_refs)):
        raise EvidenceGraphError("node contains duplicate evidence references")
    return {
        "kind": kind,
        "attributes": _canonical(attributes),
        "evidence_refs": tuple(sorted(evidence_refs)),
    }


def _edge_payload(
    source: str,
    target: str,
    kind: EvidenceEdgeKind,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    if not source or not target:
        raise EvidenceGraphError("edge endpoints must be non-empty")
    return {
        "source": source,
        "target": target,
        "kind": kind,
        "attributes": _canonical(attributes),
    }


def _graph_payload(
    nodes: tuple[EvidenceNode, ...],
    edges: tuple[EvidenceEdge, ...],
    graph_version: str,
) -> dict[str, Any]:
    return {
        "nodes": tuple(sorted(nodes, key=lambda item: item.id)),
        "edges": tuple(sorted(edges, key=lambda item: item.id)),
        "graph_version": graph_version,
    }


def _content_id(namespace: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode()).hexdigest()
    return f"{namespace}:sha256:{digest}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceGraphError("non-finite numbers are not canonical JSON")
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise EvidenceGraphError("canonical JSON object keys must be strings")
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise EvidenceGraphError(f"unsupported canonical JSON value: {type(value).__name__}")
