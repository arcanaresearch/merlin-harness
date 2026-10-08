from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


class RemoteTransport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class CredentialHandle:
    """Opaque reference to a credential held by an external secret store."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("credential handle must not be empty")


class CredentialResolver(Protocol):
    def __call__(self, handle: CredentialHandle) -> str | Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class RemoteMcpServerConfig:
    name: str
    url: str
    credential_handle: CredentialHandle
    transport: RemoteTransport = RemoteTransport.STREAMABLE_HTTP
    auth_scheme: str = "Bearer"
    tls_ca_file: Path | None = None
    connect_timeout: float = 10.0
    invocation_timeout: float = 30.0
    health_timeout: float = 5.0
    shutdown_timeout: float = 5.0
    reconnect_attempts: int = 1
    reconnect_delay: float = 0.1
    max_tools: int = 128
    max_schema_bytes: int = 256_000
    max_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if not self.name.strip() or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("remote MCP requires a name and absolute HTTPS URL")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", self.auth_scheme):
            raise ValueError("invalid authentication scheme")
        if any(
            value <= 0
            for value in (
                self.connect_timeout,
                self.invocation_timeout,
                self.health_timeout,
                self.shutdown_timeout,
                self.max_tools,
                self.max_schema_bytes,
                self.max_output_bytes,
            )
        ):
            raise ValueError("remote MCP bounds and timeouts must be positive")
        if not 0 <= self.reconnect_attempts <= 3 or not 0 <= self.reconnect_delay <= 5:
            raise ValueError("remote MCP reconnect policy exceeds hard bounds")
        if self.tls_ca_file is not None:
            object.__setattr__(self, "tls_ca_file", Path(self.tls_ca_file))


def sanitize_identity(value: str | None, *, limit: int = 200) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"[\x00-\x1f\x7f]", "?", value).strip()
    return clean[:limit] or None


def inventory_digest(tools: tuple[Any, ...]) -> str:
    document = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def bounded_mapping(value: Mapping[str, Any], *, max_bytes: int) -> dict[str, Any]:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(payload.encode()) > max_bytes:
        raise ValueError("MCP payload exceeds configured bound")
    return dict(value)
