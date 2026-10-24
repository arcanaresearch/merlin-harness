from __future__ import annotations

import pytest
from merlin_tui.wire_events import (
    CLIENT_EVENT_SCHEMA_VERSION,
    ClientEventDecodeError,
    ClientEventEnvelope,
    decode_client_event,
    encode_client_event,
)


def test_client_event_v1_round_trip_preserves_legacy_presentation() -> None:
    event = ClientEventEnvelope(kind="content", text="hello")

    encoded = encode_client_event(event)

    assert encoded == {
        "schema_version": CLIENT_EVENT_SCHEMA_VERSION,
        "presentation": {"kind": "content", "text": "hello"},
    }
    assert decode_client_event(encoded) == event


def test_client_event_decodes_legacy_bare_presentation() -> None:
    assert decode_client_event({"kind": "status", "text": "working"}) == ClientEventEnvelope(
        kind="status",
        text="working",
    )


def test_client_event_round_trip_preserves_optional_durable_identity() -> None:
    event = ClientEventEnvelope(
        kind="tool",
        text="Request sent.",
        event_id="event/7",
        durable_sequence=42,
        session_id="session/3",
        campaign_id="campaign/2",
        command_id="command/5",
        causation_id="event/6",
        correlation_id="exchange/9",
        occurred_at="2026-10-25T00:00:00Z",
        payload={"tool_call_id": "call/4", "status": "completed"},
    )

    assert decode_client_event(encode_client_event(event)) == event


def test_client_event_rejects_unknown_version_with_structured_error() -> None:
    with pytest.raises(ClientEventDecodeError) as captured:
        decode_client_event(
            {
                "schema_version": "merlin.client-event.v2",
                "presentation": {"kind": "content", "text": "future"},
            }
        )

    assert captured.value.code == "unsupported_client_event_version"
    assert captured.value.schema_version == "merlin.client-event.v2"
