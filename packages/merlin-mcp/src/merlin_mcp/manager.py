from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .remote import (
    CredentialResolver,
    RemoteMcpServerConfig,
    RemoteTransport,
    bounded_mapping,
    inventory_digest,
    sanitize_identity,
)


class McpError(RuntimeError):
    """Base error for managed MCP servers."""


class McpServerNotFoundError(McpError):
    pass


class McpServerStateError(McpError):
    pass


class McpTimeoutError(McpError):
    pass


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: Path | None = None
    startup_timeout: float = 10.0
    invocation_timeout: float = 30.0
    output_timeout: float = 5.0
    shutdown_timeout: float = 5.0

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.command.strip():
            raise ValueError("MCP server name and command must not be empty")
        if any(value <= 0 for value in self.timeouts):
            raise ValueError("MCP timeouts must be positive")
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", dict(self.env))
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd))

    @property
    def timeouts(self) -> tuple[float, float, float, float]:
        return (
            self.startup_timeout,
            self.invocation_timeout,
            self.output_timeout,
            self.shutdown_timeout,
        )


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    server_name: str
    name: str
    description: str | None
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class McpCallResult:
    server_name: str
    tool_name: str
    content: tuple[Mapping[str, Any], ...]
    structured_content: Mapping[str, Any] | None
    is_error: bool


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    name: str
    running: bool
    tools: tuple[McpToolInfo, ...]
    error: str | None = None
    quarantined: bool = False
    inventory_digest: str | None = None
    server_identity: str | None = None


@dataclass(slots=True)
class _Operation:
    kind: str
    future: asyncio.Future[Any]
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None


@dataclass(slots=True)
class _Runtime:
    config: McpServerConfig | RemoteMcpServerConfig
    queue: asyncio.Queue[_Operation]
    task: asyncio.Task[None]
    ready: asyncio.Future[tuple[McpToolInfo, ...]]
    tools: tuple[McpToolInfo, ...] = ()
    error: str | None = None
    inventory_digest: str | None = None
    server_identity: str | None = None
    secret: str | None = field(default=None, repr=False)


class McpManager:
    """Own named local stdio MCP servers and serialize work per subprocess."""

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        remote_http_client_factory: Callable[
            [RemoteMcpServerConfig, Mapping[str, str]],
            AbstractAsyncContextManager[httpx2.AsyncClient],
        ]
        | None = None,
    ) -> None:
        self._configs: dict[str, McpServerConfig | RemoteMcpServerConfig] = {}
        self._runtimes: dict[str, _Runtime] = {}
        self._credential_resolver = credential_resolver
        self._remote_http_client_factory = remote_http_client_factory
        self._admitted_inventories: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> McpManager:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def set_credential_resolver(self, resolver: CredentialResolver) -> None:
        if any(
            isinstance(runtime.config, RemoteMcpServerConfig) and not runtime.task.done()
            for runtime in self._runtimes.values()
        ):
            raise McpServerStateError("cannot replace credentials while a remote server is running")
        self._credential_resolver = resolver

    def add(self, config: McpServerConfig | RemoteMcpServerConfig) -> None:
        if config.name in self._configs:
            raise McpServerStateError(f"MCP server {config.name!r} already exists")
        self._configs[config.name] = config

    async def add_server(
        self, config: McpServerConfig | RemoteMcpServerConfig, *, start: bool = False
    ) -> None:
        self.add(config)
        if start:
            try:
                await self.start(config.name)
            except BaseException:
                self._configs.pop(config.name, None)
                raise

    async def remove(self, name: str) -> None:
        self._config(name)
        await self.stop(name)
        self._configs.pop(name)
        self._admitted_inventories.pop(name, None)

    async def remove_server(self, name: str) -> None:
        await self.remove(name)

    async def start(self, name: str) -> tuple[McpToolInfo, ...]:
        config = self._config(name)
        async with self._lock:
            existing = self._runtimes.get(name)
            if existing and not existing.task.done():
                return existing.tools
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[_Operation] = asyncio.Queue()
            ready: asyncio.Future[tuple[McpToolInfo, ...]] = loop.create_future()
            task = asyncio.create_task(self._run(config, queue, ready), name=f"mcp:{name}")
            runtime = _Runtime(config, queue, task, ready)
            self._runtimes[name] = runtime
            task.add_done_callback(lambda done, server=name: self._record_exit(server, done))
        try:
            startup_timeout = (
                config.connect_timeout
                if isinstance(config, RemoteMcpServerConfig)
                else config.startup_timeout
            )
            runtime.tools = await asyncio.wait_for(asyncio.shield(ready), startup_timeout)
            return runtime.tools
        except TimeoutError as error:
            await self._terminate(runtime)
            raise McpTimeoutError(f"MCP server {name!r} startup timed out") from error
        except Exception as error:
            runtime.error = self._safe_error(error, config, secrets=(runtime.secret,))
            await self._terminate(runtime)
            raise McpServerStateError(
                f"MCP server {name!r} failed to start: {runtime.error}"
            ) from error

    async def check(self, name: str) -> McpServerInfo:
        config = self._config(name)
        runtime = self._runtimes.get(name)
        if runtime is None or runtime.task.done():
            return McpServerInfo(name, False, (), runtime.error if runtime else None)
        try:
            tools = await self._request(
                runtime,
                _Operation("list", self._future()),
                config.health_timeout
                if isinstance(config, RemoteMcpServerConfig)
                else config.output_timeout,
            )
            runtime.tools = tools
            return self._server_info(runtime, running=True)
        except McpError as error:
            return McpServerInfo(
                name, False, (), self._safe_error(error, config, secrets=(runtime.secret,))
            )

    async def check_server(self, name: str) -> McpServerInfo:
        return await self.check(name)

    async def restart(self, name: str) -> tuple[McpToolInfo, ...]:
        await self.stop(name)
        return await self.start(name)

    async def restart_server(self, name: str) -> tuple[McpToolInfo, ...]:
        return await self.restart(name)

    async def stop(self, name: str) -> None:
        config = self._config(name)
        runtime = self._runtimes.pop(name, None)
        if runtime is None or runtime.task.done():
            return
        operation = _Operation("stop", self._future())
        try:
            await self._request(runtime, operation, config.shutdown_timeout)
            await asyncio.wait_for(asyncio.shield(runtime.task), config.shutdown_timeout)
        except (TimeoutError, McpTimeoutError):
            await self._terminate(runtime)

    async def stop_server(self, name: str) -> None:
        await self.stop(name)

    async def list_tools(self, server_name: str) -> tuple[McpToolInfo, ...]:
        runtime = await self._running(server_name)
        config = runtime.config
        tools = await self._request(
            runtime,
            _Operation("list", self._future()),
            config.health_timeout
            if isinstance(config, RemoteMcpServerConfig)
            else config.output_timeout,
        )
        runtime.tools = tools
        return tools

    async def invoke(
        self, server_name: str, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> McpCallResult:
        runtime = await self._running(server_name)
        if isinstance(runtime.config, RemoteMcpServerConfig):
            admitted = self._admitted_inventories.get(server_name)
            if admitted != runtime.inventory_digest:
                raise McpServerStateError(
                    f"remote MCP server {server_name!r} inventory is quarantined"
                )
        operation = _Operation("call", self._future(), tool_name, dict(arguments or {}))
        return await self._request(runtime, operation, runtime.config.invocation_timeout)

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> McpCallResult:
        return await self.invoke(server_name, tool_name, arguments)

    def list_servers(self) -> tuple[McpServerInfo, ...]:
        return tuple(
            self._server_info(runtime, running=not runtime.task.done())
            if (runtime := self._runtimes.get(name))
            else McpServerInfo(name, False, ())
            for name in sorted(self._configs)
        )

    def server_config(self, name: str) -> McpServerConfig | RemoteMcpServerConfig:
        return self._config(name)

    def admit_remote_inventory(self, name: str, digest: str) -> None:
        config = self._config(name)
        if not isinstance(config, RemoteMcpServerConfig):
            raise McpServerStateError(f"MCP server {name!r} is not remote")
        runtime = self._runtimes.get(name)
        if runtime is None or runtime.inventory_digest != digest:
            raise McpServerStateError("only the currently observed inventory can be admitted")
        self._admitted_inventories[name] = digest

    async def close(self) -> None:
        for name in tuple(self._runtimes):
            await self.stop(name)

    async def _running(self, name: str) -> _Runtime:
        self._config(name)
        runtime = self._runtimes.get(name)
        if runtime is None or runtime.task.done():
            await self.start(name)
            runtime = self._runtimes[name]
        return runtime

    async def _run(
        self,
        config: McpServerConfig | RemoteMcpServerConfig,
        queue: asyncio.Queue[_Operation],
        ready: asyncio.Future[tuple[McpToolInfo, ...]],
    ) -> None:
        if isinstance(config, RemoteMcpServerConfig):
            await self._run_remote(config, queue, ready)
            return
        parameters = StdioServerParameters(
            command=config.command, args=list(config.args), env=dict(config.env), cwd=config.cwd
        )
        try:
            async with Client(parameters) as client:
                initial = self._tools(config.name, await client.list_tools(cache_mode="refresh"))
                if not ready.done():
                    ready.set_result(initial)
                while True:
                    operation = await queue.get()
                    try:
                        if operation.kind == "stop":
                            operation.future.set_result(None)
                            return
                        if operation.kind == "list":
                            result = self._tools(
                                config.name, await client.list_tools(cache_mode="refresh")
                            )
                        else:
                            raw = await client.call_tool(
                                operation.tool_name or "", dict(operation.arguments or {})
                            )
                            result = self._call_result(config.name, operation.tool_name or "", raw)
                        if not operation.future.done():
                            operation.future.set_result(result)
                    except BaseException as error:
                        if not operation.future.done():
                            operation.future.set_exception(error)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    async def _run_remote(
        self,
        config: RemoteMcpServerConfig,
        queue: asyncio.Queue[_Operation],
        ready: asyncio.Future[tuple[McpToolInfo, ...]],
    ) -> None:
        if self._credential_resolver is None:
            raise McpServerStateError("remote MCP credential resolver is not configured")
        token_value = self._credential_resolver(config.credential_handle)
        token = await token_value if inspect.isawaitable(token_value) else token_value
        if not token:
            raise McpServerStateError("remote MCP credential resolver returned an empty value")
        self._runtimes[config.name].secret = token
        headers = {"Authorization": f"{config.auth_scheme} {token}"}
        last_error: BaseException | None = None
        for attempt in range(config.reconnect_attempts + 1):
            try:
                async with self._http_client(config, headers) as http_client:
                    transport = (
                        streamable_http_client(config.url, http_client=http_client)
                        if config.transport is RemoteTransport.STREAMABLE_HTTP
                        else sse_client(
                            config.url,
                            headers=headers,
                            timeout=config.connect_timeout,
                            sse_read_timeout=config.invocation_timeout,
                            httpx_client_factory=self._sse_http_client_factory(config),
                        )
                    )
                    async with Client(transport) as client:
                        raw_tools = await client.list_tools(cache_mode="refresh")
                        initial = self._tools(config.name, raw_tools, config=config)
                        runtime = self._runtimes[config.name]
                        runtime.inventory_digest = inventory_digest(initial)
                        info = getattr(client, "server_info", None)
                        runtime.server_identity = sanitize_identity(
                            getattr(info, "name", None) if info else None
                        )
                        if not ready.done():
                            ready.set_result(initial)
                        await self._serve_remote(client, config, queue)
                        return
            except BaseException as error:
                last_error = error
                if (
                    isinstance(error, asyncio.CancelledError)
                    or attempt >= config.reconnect_attempts
                ):
                    break
                await asyncio.sleep(config.reconnect_delay)
        assert last_error is not None
        if not ready.done():
            ready.set_exception(last_error)
        raise last_error

    async def _serve_remote(
        self, client: Client, config: RemoteMcpServerConfig, queue: asyncio.Queue[_Operation]
    ) -> None:
        while True:
            operation = await queue.get()
            try:
                if operation.kind == "stop":
                    operation.future.set_result(None)
                    return
                if operation.kind == "list":
                    result = self._tools(
                        config.name,
                        await client.list_tools(cache_mode="refresh"),
                        config=config,
                    )
                    runtime = self._runtimes[config.name]
                    runtime.inventory_digest = inventory_digest(result)
                else:
                    raw = await client.call_tool(
                        operation.tool_name or "", dict(operation.arguments or {})
                    )
                    result = self._call_result(
                        config.name, operation.tool_name or "", raw, config=config
                    )
                if not operation.future.done():
                    operation.future.set_result(result)
            except BaseException as error:
                if not operation.future.done():
                    operation.future.set_exception(error)
                raise

    def _http_client(
        self, config: RemoteMcpServerConfig, headers: Mapping[str, str]
    ) -> AbstractAsyncContextManager[httpx2.AsyncClient]:
        if self._remote_http_client_factory is not None:
            return self._remote_http_client_factory(config, headers)
        verify: str | bool = str(config.tls_ca_file) if config.tls_ca_file else True
        return httpx2.AsyncClient(
            headers=headers,
            verify=verify,
            timeout=httpx2.Timeout(config.connect_timeout),
            follow_redirects=False,
            trust_env=False,
            limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2),
        )

    @staticmethod
    def _sse_http_client_factory(
        config: RemoteMcpServerConfig,
    ) -> Callable[..., httpx2.AsyncClient]:
        def create(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            verify: str | bool = str(config.tls_ca_file) if config.tls_ca_file else True
            return httpx2.AsyncClient(
                headers=headers,
                timeout=timeout or httpx2.Timeout(config.connect_timeout),
                auth=auth,
                verify=verify,
                follow_redirects=False,
                trust_env=False,
                limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2),
            )

        return create

    async def _request(
        self, runtime: _Runtime, operation: _Operation, deadline_seconds: float
    ) -> Any:
        if runtime.task.done():
            raise McpServerStateError(f"MCP server {runtime.config.name!r} is not running")
        await runtime.queue.put(operation)
        try:
            return await asyncio.wait_for(asyncio.shield(operation.future), deadline_seconds)
        except TimeoutError as error:
            operation.future.cancel()
            await self._terminate(runtime)
            raise McpTimeoutError(
                f"MCP server {runtime.config.name!r} operation timed out"
            ) from error
        except asyncio.CancelledError:
            operation.future.cancel()
            await self._terminate(runtime)
            raise

    async def _terminate(self, runtime: _Runtime) -> None:
        runtime.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(runtime.task), runtime.config.shutdown_timeout)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass

    def _record_exit(self, name: str, task: asyncio.Task[None]) -> None:
        runtime = self._runtimes.get(name)
        if runtime is None or task.cancelled():
            return
        error = task.exception()
        if error is not None:
            runtime.error = self._safe_error(error, runtime.config, secrets=(runtime.secret,))

    def _config(self, name: str) -> McpServerConfig | RemoteMcpServerConfig:
        try:
            return self._configs[name]
        except KeyError as error:
            raise McpServerNotFoundError(f"unknown MCP server {name!r}") from error

    @staticmethod
    def _future() -> asyncio.Future[Any]:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    def _tools(
        server_name: str, result: Any, *, config: RemoteMcpServerConfig | None = None
    ) -> tuple[McpToolInfo, ...]:
        if config is not None and len(result.tools) > config.max_tools:
            raise McpServerStateError("remote MCP tool inventory exceeds configured bound")
        tools = tuple(
            McpToolInfo(
                server_name,
                sanitize_identity(tool.name, limit=128) or "invalid-tool",
                sanitize_identity(tool.description, limit=2_000),
                bounded_mapping(tool.input_schema, max_bytes=config.max_schema_bytes)
                if config
                else dict(tool.input_schema),
            )
            for tool in result.tools
        )
        return tools

    @staticmethod
    def _call_result(
        server_name: str,
        tool_name: str,
        result: Any,
        *,
        config: RemoteMcpServerConfig | None = None,
    ) -> McpCallResult:
        content = tuple(item.model_dump(mode="json", exclude_none=True) for item in result.content)
        structured = result.structured_content
        if config is not None:
            bounded_mapping({"content": content}, max_bytes=config.max_output_bytes)
            if structured is not None:
                bounded_mapping(structured, max_bytes=config.max_output_bytes)
        return McpCallResult(
            server_name,
            tool_name,
            content,
            dict(structured) if structured is not None else None,
            bool(result.is_error),
        )

    @staticmethod
    def _safe_error(
        error: BaseException,
        config: McpServerConfig | RemoteMcpServerConfig,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> str:
        message = str(error)
        configured = tuple(config.env.values()) if isinstance(config, McpServerConfig) else ()
        for secret in configured + secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        message = re.sub(
            r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|"
            r"token\s*[:=]\s*|api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            message,
        )
        message = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", message)
        return message[:1000] or type(error).__name__

    def _server_info(self, runtime: _Runtime, *, running: bool) -> McpServerInfo:
        remote = isinstance(runtime.config, RemoteMcpServerConfig)
        quarantined = remote and (
            self._admitted_inventories.get(runtime.config.name) != runtime.inventory_digest
        )
        return McpServerInfo(
            runtime.config.name,
            running,
            runtime.tools,
            runtime.error,
            quarantined,
            runtime.inventory_digest,
            runtime.server_identity,
        )


__all__ = [
    "McpCallResult",
    "McpError",
    "McpManager",
    "McpServerConfig",
    "McpServerInfo",
    "McpServerNotFoundError",
    "McpServerStateError",
    "McpTimeoutError",
    "McpToolInfo",
]
