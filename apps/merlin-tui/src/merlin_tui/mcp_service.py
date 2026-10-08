from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from merlin_config import CredentialStore, McpRuntimeTuning
from merlin_contracts import ToolResult
from merlin_mcp import (
    CredentialHandle,
    McpCallResult,
    McpManager,
    McpServerConfig,
    McpServerInfo,
    McpServerStateError,
    RemoteMcpServerConfig,
    RemoteTransport,
)
from merlin_tools import McpToolInfo, ToolBroker, discover_mcp_tools

from .configuration import McpConfiguration


class McpService:
    """Own MCP configuration and keep the shared tool broker in sync."""

    def __init__(
        self,
        manager: McpManager,
        tools: ToolBroker,
        configuration: McpConfiguration | None = None,
        credential_store: CredentialStore | None = None,
        tuning: McpRuntimeTuning | None = None,
    ) -> None:
        self.manager = manager
        self.tools = tools
        self.configuration = configuration
        self.credential_store = credential_store or CredentialStore(
            configuration.paths if configuration is not None else None
        )
        self.tuning = tuning or McpRuntimeTuning()
        self.recent_calls: list[str] = []
        self.recent_call_audit: list[Mapping[str, str]] = []
        self._disabled: set[str] = set()
        if hasattr(self.manager, "set_credential_resolver"):
            self.manager.set_credential_resolver(self._resolve_credential)

    def load(self, values: object) -> None:
        if not isinstance(values, Mapping):
            return
        servers = values.get("servers", {})
        if not isinstance(servers, Mapping):
            return
        for name, raw in servers.items():
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                continue
            command = raw.get("command")
            args = raw.get("args", ())
            cwd = raw.get("cwd")
            remote_url = raw.get("remote_url")
            disabled = raw.get("disabled") is True
            if isinstance(remote_url, str):
                handle = raw.get("credential_handle")
                transport = raw.get("transport", RemoteTransport.STREAMABLE_HTTP.value)
                reconnect = raw.get("reconnect_attempts", self.tuning.reconnect_attempts)
                if not isinstance(handle, str) or not isinstance(transport, str):
                    continue
                try:
                    remote = RemoteMcpServerConfig(
                        name=name,
                        url=remote_url,
                        credential_handle=CredentialHandle(handle),
                        transport=RemoteTransport(transport),
                        reconnect_attempts=int(reconnect),
                        connect_timeout=self.tuning.connect_timeout_seconds,
                        invocation_timeout=self.tuning.invocation_timeout_seconds,
                        health_timeout=self.tuning.health_timeout_seconds,
                        shutdown_timeout=self.tuning.shutdown_timeout_seconds,
                        reconnect_delay=self.tuning.reconnect_delay_seconds,
                        max_tools=self.tuning.max_tools,
                        max_schema_bytes=self.tuning.max_schema_bytes,
                        max_output_bytes=self.tuning.max_output_bytes,
                    )
                except (ValueError, TypeError):
                    continue
                self.manager.add(remote)
                if disabled:
                    self._disabled.add(name)
                continue
            if isinstance(command, str) and isinstance(args, list):
                self.manager.add(
                    McpServerConfig(
                        name=name,
                        command=command,
                        args=tuple(str(arg) for arg in args),
                        cwd=Path(cwd) if isinstance(cwd, str) else None,
                        startup_timeout=self.tuning.startup_timeout_seconds,
                        invocation_timeout=self.tuning.invocation_timeout_seconds,
                        output_timeout=self.tuning.output_timeout_seconds,
                        shutdown_timeout=self.tuning.shutdown_timeout_seconds,
                    )
                )
                if disabled:
                    self._disabled.add(name)

    async def start_configured(self) -> None:
        for server in self.manager.list_servers():
            if server.name not in self._disabled:
                await self._start_and_refresh(server.name)

    async def list_servers(self) -> Sequence[Mapping[str, Any]]:
        return tuple(self._server_dict(server) for server in self.manager.list_servers())

    async def list_tools(self, server_name: str) -> Sequence[McpToolInfo]:
        return tuple(
            McpToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
            )
            for tool in await self.manager.list_tools(server_name)
        )

    async def add_server(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        if len(arguments) < 2:
            raise ValueError(
                "usage: /mcp add NAME [--cwd PATH] [--env KEY=VALUE]... COMMAND [ARGS...]"
            )
        config = self._parse_config(arguments)
        await self.manager.add_server(config, start=True)
        if self.configuration is not None:
            if isinstance(config, RemoteMcpServerConfig):
                self.configuration.save_remote_server(
                    config.name,
                    config.url,
                    config.credential_handle.value,
                    transport=config.transport.value,
                    reconnect_attempts=config.reconnect_attempts,
                )
            else:
                self.configuration.save_server(config.name, config.command, config.args, config.cwd)
        checked = await self.manager.check(config.name)
        if not checked.quarantined:
            await self._refresh(config.name)
        return self._server_dict(checked)

    async def add_remote_with_secret(
        self, name: str, url: str, credential_handle: str, secret: str
    ) -> Mapping[str, Any]:
        """Store the bearer secret separately and admit only its opaque handle to config."""
        if not all(value.strip() for value in (name, url, credential_handle, secret)):
            raise ValueError("name, URL, credential handle, and credential are required")
        credentials = dict(self.credential_store.load(environ={}))
        credentials[credential_handle] = secret
        self.credential_store.save(credentials)
        return await self.add_server((name, "--remote", url, "--credential", credential_handle))

    async def remove_server(self, name: str) -> Mapping[str, Any]:
        await self.manager.remove(name)
        if self.configuration is not None:
            self.configuration.remove_server(name)
        self.tools.replace_by_prefix(f"mcp__{name}__", ())
        self._disabled.discard(name)
        return {"name": name, "removed": True}

    async def disable_server(self, name: str) -> Mapping[str, Any]:
        await self.manager.stop(name)
        self._disabled.add(name)
        self.tools.replace_by_prefix(f"mcp__{name}__", ())
        if self.configuration is not None:
            self.configuration.set_disabled(name, True)
        return {"name": name, "status": "disabled"}

    async def admit_server(self, name: str, digest: str) -> Mapping[str, Any]:
        self.manager.admit_remote_inventory(name, digest)
        await self._refresh(name)
        return self._server_dict(await self.manager.check(name))

    async def check_server(self, name: str) -> Mapping[str, Any]:
        server = await self.manager.check(name)
        if server.running and not server.quarantined:
            await self._refresh(name)
        elif server.quarantined:
            self.tools.replace_by_prefix(f"mcp__{name}__", ())
        return self._server_dict(server)

    async def restart_server(self, name: str) -> Mapping[str, Any]:
        if name in self._disabled:
            raise McpServerStateError(f"MCP server {name!r} is disabled")
        await self.manager.restart(name)
        checked = await self.manager.check(name)
        if not checked.quarantined:
            await self._refresh(name)
        return self._server_dict(checked)

    async def invoke(
        self, server_name: str, tool_name: str, arguments: Mapping[str, object]
    ) -> ToolResult:
        result = await self.manager.invoke(server_name, tool_name, arguments)
        content = self._content(result)
        state = "error" if result.is_error else "ok"
        self.recent_calls.append(f"{server_name}.{tool_name}: {state}")
        del self.recent_calls[:-20]
        self.recent_call_audit.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "server": server_name,
                "tool": tool_name,
                "status": state,
            }
        )
        del self.recent_call_audit[:-20]
        return ToolResult(
            content=content,
            is_error=result.is_error,
            metadata={"server": server_name, "tool": tool_name},
        )

    async def close(self) -> None:
        await self.manager.close()

    async def _start_and_refresh(self, name: str) -> None:
        await self.manager.start(name)
        checked = await self.manager.check(name)
        if not checked.quarantined:
            await self._refresh(name)

    async def _refresh(self, name: str) -> None:
        adapters = await discover_mcp_tools(self, name)
        self.tools.replace_by_prefix(f"mcp__{name}__", adapters)

    def _server_dict(self, server: McpServerInfo) -> Mapping[str, Any]:
        config = (
            self.manager.server_config(server.name)
            if hasattr(self.manager, "server_config")
            else None
        )
        return {
            "name": server.name,
            "status": "running" if server.running else "stopped",
            "tools": tuple(tool.name for tool in server.tools),
            "recent_calls": tuple(
                call for call in self.recent_calls if call.startswith(f"{server.name}.")
            ),
            "stderr": server.error,
            "disabled": server.name in self._disabled,
            "admission": "quarantined" if server.quarantined else "admitted",
            "inventory_digest": server.inventory_digest,
            "identity": server.server_identity,
            "health": "healthy" if server.running and not server.error else "unavailable",
            "reconnect": self._reconnect_policy(server.name),
            "recent_call_audit": tuple(
                record for record in self.recent_call_audit if record["server"] == server.name
            ),
            "transport": (
                config.transport.value if isinstance(config, RemoteMcpServerConfig) else "stdio"
            ),
        }

    def _parse_config(
        self,
        arguments: Sequence[str],
    ) -> McpServerConfig | RemoteMcpServerConfig:
        name = arguments[0]
        if len(arguments) >= 5 and arguments[1] == "--remote":
            if arguments[3] != "--credential":
                raise ValueError(
                    "usage: /mcp add NAME --remote HTTPS_URL --credential HANDLE "
                    "[--transport streamable_http|sse]"
                )
            transport = RemoteTransport.STREAMABLE_HTTP
            reconnect_attempts = self.tuning.reconnect_attempts
            index = 5
            while index < len(arguments):
                if arguments[index] == "--transport" and index + 1 < len(arguments):
                    transport = RemoteTransport(arguments[index + 1])
                    index += 2
                elif arguments[index] == "--reconnect" and index + 1 < len(arguments):
                    reconnect_attempts = int(arguments[index + 1])
                    index += 2
                else:
                    raise ValueError(f"unknown remote MCP option {arguments[index]!r}")
            return RemoteMcpServerConfig(
                name=name,
                url=arguments[2],
                credential_handle=CredentialHandle(arguments[4]),
                transport=transport,
                reconnect_attempts=reconnect_attempts,
                connect_timeout=self.tuning.connect_timeout_seconds,
                invocation_timeout=self.tuning.invocation_timeout_seconds,
                health_timeout=self.tuning.health_timeout_seconds,
                shutdown_timeout=self.tuning.shutdown_timeout_seconds,
                reconnect_delay=self.tuning.reconnect_delay_seconds,
                max_tools=self.tuning.max_tools,
                max_schema_bytes=self.tuning.max_schema_bytes,
                max_output_bytes=self.tuning.max_output_bytes,
            )
        cwd: Path | None = None
        env: dict[str, str] = {}
        index = 1
        while index < len(arguments):
            option = arguments[index]
            if option == "--":
                index += 1
                break
            if option == "--cwd":
                if index + 1 >= len(arguments):
                    raise ValueError("--cwd requires a path")
                cwd = Path(arguments[index + 1])
                index += 2
                continue
            if option == "--env":
                if index + 1 >= len(arguments) or "=" not in arguments[index + 1]:
                    raise ValueError("--env requires KEY=VALUE")
                key, value = arguments[index + 1].split("=", 1)
                if not key:
                    raise ValueError("--env requires a non-empty key")
                env[key] = value
                index += 2
                continue
            break
        if index >= len(arguments):
            raise ValueError("MCP server command is required")
        return McpServerConfig(
            name=name,
            command=arguments[index],
            args=tuple(arguments[index + 1 :]),
            env=env,
            cwd=cwd,
            startup_timeout=self.tuning.startup_timeout_seconds,
            invocation_timeout=self.tuning.invocation_timeout_seconds,
            output_timeout=self.tuning.output_timeout_seconds,
            shutdown_timeout=self.tuning.shutdown_timeout_seconds,
        )

    def _resolve_credential(self, handle: CredentialHandle) -> str:
        try:
            return self.credential_store.load()[handle.value]
        except KeyError as error:
            raise McpServerStateError("remote MCP credential is unavailable") from error

    def _reconnect_policy(self, name: str) -> Mapping[str, object] | None:
        if not hasattr(self.manager, "server_config"):
            return None
        config = self.manager.server_config(name)
        if not isinstance(config, RemoteMcpServerConfig):
            return None
        return {
            "attempts": config.reconnect_attempts,
            "delay_seconds": config.reconnect_delay,
        }

    @staticmethod
    def _content(result: McpCallResult) -> str:
        if result.structured_content is not None:
            return json.dumps(result.structured_content, ensure_ascii=False)
        texts = [str(item.get("text")) for item in result.content if item.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result.content, ensure_ascii=False)
