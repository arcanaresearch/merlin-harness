from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from merlin_contracts import ToolContext, ToolResult, ToolSpec


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: dict[str, object]
    effect: str = "unknown"


class McpManager(Protocol):
    """The portion of an MCP manager needed by the tool adapter."""

    async def list_tools(self, server_name: str) -> Sequence[McpToolInfo]: ...

    async def invoke(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> ToolResult: ...


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    if not server_name or not tool_name:
        raise ValueError("MCP server and tool names cannot be empty")
    return f"mcp__{server_name}__{tool_name}"


class McpTool:
    """Adapt one manager-owned MCP tool to Merlin's ordinary Tool API."""

    def __init__(
        self,
        manager: McpManager,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: Mapping[str, object],
        effect: str = "unknown",
    ) -> None:
        self._manager = manager
        self._server_name = server_name
        self._tool_name = tool_name
        self._spec = ToolSpec(
            name=mcp_tool_name(server_name, tool_name),
            description=description,
            input_schema=dict(input_schema),
        )
        self.effect = effect or "unknown"

    def describe(self) -> ToolSpec:
        return self._spec

    async def invoke(
        self,
        context: ToolContext,
        arguments: dict[str, object],
    ) -> ToolResult:
        del context
        result = await self._manager.invoke(
            self._server_name,
            self._tool_name,
            arguments,
        )
        metadata = dict(result.metadata)
        metadata.setdefault("effect", self.effect)
        return result.model_copy(update={"metadata": metadata})


async def discover_mcp_tools(
    manager: McpManager,
    server_name: str,
) -> tuple[McpTool, ...]:
    infos = await manager.list_tools(server_name)
    return tuple(
        McpTool(
            manager,
            server_name=server_name,
            tool_name=info.name,
            description=info.description,
            input_schema=info.input_schema,
            effect=info.effect,
        )
        for info in infos
    )
