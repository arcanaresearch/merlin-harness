"""Optional typed portable-memory boundary for durable workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .specialists import WorkerAssignment


@dataclass(frozen=True, slots=True)
class PortableMemoryQuery:
    session_id: str
    task_id: str
    repository_id: str
    target_id: str
    provider_id: str
    operator_id: str
    scope_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortableMemoryRecord:
    id: str
    kind: str
    content: str
    provenance_source: str
    reviewed_by: tuple[str, ...]
    digest: str


class PortableMemoryProvider(Protocol):
    async def retrieve(self, query: PortableMemoryQuery) -> tuple[PortableMemoryRecord, ...]: ...

    async def record_turn_use(
        self,
        query: PortableMemoryQuery,
        turn_id: str,
        record_ids: tuple[str, ...],
    ) -> None: ...


class PortableMemoryScope(Protocol):
    def __call__(self, session_id: str, assignment: WorkerAssignment) -> PortableMemoryQuery: ...
