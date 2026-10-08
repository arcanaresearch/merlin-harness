from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from merlin_contracts import ToolContext, ToolResult, ToolSpec

from .knowledge import MAX_SELECTED_ENTRIES, select_knowledge


class KnowledgeSearchTool:
    """Retrieve another bounded projection from the authenticated built-in packs."""

    effect = "workspace_read"

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="knowledge_search",
            description=(
                "Search Merlin's authenticated knowledge packs. Returns at most 12 relevant "
                "entries per call; call again with a refined query when more context is needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SELECTED_ENTRIES,
                        "default": MAX_SELECTED_ENTRIES,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def invoke(self, _context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = arguments.get("limit", MAX_SELECTED_ENTRIES)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        entries = select_knowledge(query, limit=limit)
        payload = [
            {
                "pack_id": entry.pack_id,
                "pack_version": entry.pack_version,
                "entry_id": entry.entry_id,
                "entry_digest": entry.entry_digest,
                "kind": entry.kind,
                "content": entry.content,
                "specialist_tags": entry.specialist_tags,
                "target_tags": entry.target_tags,
                "applicability_tags": entry.applicability_tags,
            }
            for entry in entries
        ]
        return ToolResult(
            content=json.dumps(payload, sort_keys=True),
            metadata={"query": query, "returned": len(entries), "limit": limit},
        )
