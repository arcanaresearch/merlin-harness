"""Versioned wire envelope for daemon-to-client presentation events.

This module is intentionally independent of the current ``StreamEvent``
transport. It provides the compatibility boundary needed to migrate the wire
protocol without changing existing renderers in one step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

CLIENT_EVENT_SCHEMA_VERSION = "merlin.client-event.v1"


class ClientEventDecodeError(ValueError):
    """A client-event frame is malformed or uses an unsupported version."""

    def __init__(self, code: str, message: str, *, schema_version: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.schema_version = schema_version


@dataclass(frozen=True, slots=True)
class ClientEventEnvelope:
    """Canonical event identity wrapped around the legacy presentation pair."""

    kind: str
    text: str
    event_id: str | None = None
    durable_sequence: int | None = None
    session_id: str | None = None
    campaign_id: str | None = None
    command_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str | None = None
    payload: Mapping[str, object] | None = None
    schema_version: str = CLIENT_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CLIENT_EVENT_SCHEMA_VERSION:
            raise ClientEventDecodeError(
                "unsupported_client_event_version",
                f"unsupported client-event schema version: {self.schema_version!r}",
                schema_version=self.schema_version,
            )
        _require_text("kind", self.kind)
        if not isinstance(self.text, str):
            raise ClientEventDecodeError("invalid_client_event", "text must be a string")
        for name in (
            "event_id",
            "session_id",
            "campaign_id",
            "command_id",
            "causation_id",
            "correlation_id",
            "occurred_at",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(name, value)
        if self.durable_sequence is not None and (
            isinstance(self.durable_sequence, bool)
            or not isinstance(self.durable_sequence, int)
            or self.durable_sequence < 0
        ):
            raise ClientEventDecodeError(
                "invalid_client_event",
                "durable_sequence must be a non-negative integer",
            )
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise ClientEventDecodeError("invalid_client_event", "payload must be an object")


def encode_client_event(event: ClientEventEnvelope) -> dict[str, object]:
    """Encode a v1 envelope while retaining the legacy presentation unchanged."""

    encoded: dict[str, object] = {
        "schema_version": event.schema_version,
        "presentation": {"kind": event.kind, "text": event.text},
    }
    for name in (
        "event_id",
        "durable_sequence",
        "session_id",
        "campaign_id",
        "command_id",
        "causation_id",
        "correlation_id",
        "occurred_at",
        "payload",
    ):
        value = getattr(event, name)
        if value is not None:
            encoded[name] = dict(value) if name == "payload" else value
    return encoded


def decode_client_event(value: Mapping[str, object]) -> ClientEventEnvelope:
    """Decode either a v1 envelope or the legacy bare ``kind``/``text`` pair."""

    if not isinstance(value, Mapping):
        raise ClientEventDecodeError("invalid_client_event", "client event must be an object")
    if "schema_version" not in value:
        return ClientEventEnvelope(
            kind=_required_string(value, "kind", allow_empty=False),
            text=_required_string(value, "text", allow_empty=True),
        )

    schema_version = value["schema_version"]
    if schema_version != CLIENT_EVENT_SCHEMA_VERSION:
        raise ClientEventDecodeError(
            "unsupported_client_event_version",
            f"unsupported client-event schema version: {schema_version!r}",
            schema_version=schema_version,
        )
    presentation = value.get("presentation")
    if not isinstance(presentation, Mapping):
        raise ClientEventDecodeError(
            "invalid_client_event",
            "presentation must be an object",
        )
    payload = value.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise ClientEventDecodeError("invalid_client_event", "payload must be an object")
    return ClientEventEnvelope(
        kind=_required_string(presentation, "kind", allow_empty=False),
        text=_required_string(presentation, "text", allow_empty=True),
        event_id=_optional_string(value, "event_id"),
        durable_sequence=_optional_nonnegative_integer(value, "durable_sequence"),
        session_id=_optional_string(value, "session_id"),
        campaign_id=_optional_string(value, "campaign_id"),
        command_id=_optional_string(value, "command_id"),
        causation_id=_optional_string(value, "causation_id"),
        correlation_id=_optional_string(value, "correlation_id"),
        occurred_at=_optional_string(value, "occurred_at"),
        payload=dict(payload) if payload is not None else None,
    )


def _required_string(value: Mapping[str, object], key: str, *, allow_empty: bool) -> str:
    item = value.get(key)
    if not isinstance(item, str) or (not allow_empty and not item.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ClientEventDecodeError("invalid_client_event", f"{key} must be {qualifier}")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ClientEventDecodeError(
            "invalid_client_event",
            f"{key} must be a non-empty string",
        )
    return item


def _optional_nonnegative_integer(value: Mapping[str, object], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ClientEventDecodeError(
            "invalid_client_event",
            f"{key} must be a non-negative integer",
        )
    return item


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ClientEventDecodeError(
            "invalid_client_event",
            f"{name} must be a non-empty string",
        )
