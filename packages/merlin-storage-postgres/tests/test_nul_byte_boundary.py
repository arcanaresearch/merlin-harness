from pathlib import Path

from merlin_contracts import ToolEffectRecord, ToolEffectState
from merlin_storage_postgres import (
    Database,
    DatabaseConfig,
    MessageRepository,
    MigrationRunner,
    SessionRepository,
    ToolEffectRepository,
)
from merlin_storage_postgres.sanitize import escape_nul


def test_escape_nul_preserves_legitimate_content() -> None:
    assert escape_nul("plain") == "plain"
    assert escape_nul(None) is None
    assert escape_nul("a\x00b") == "a\\u0000b"
    assert escape_nul({"input": "x\x00y", "nested": ["\x00", 1, None]}) == {
        "input": "x\\u0000y",
        "nested": ["\\u0000", 1, None],
    }
    assert escape_nul(("a\x00",)) == ["a\\u0000"]
    assert escape_nul("already escaped \\u0000 text") == "already escaped \\u0000 text"


def test_message_append_with_nul_bytes(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("nul boundary")
            messages = MessageRepository(connection)

            messages.append(
                session.id,
                "assistant",
                "",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "target_request", "arguments": {"input": "a\x00b"}},
                    }
                ],
            )
            messages.append(
                session.id,
                "tool",
                "result with \x00 byte",
                name="target_request",
                tool_call_id="call-1",
            )

            loaded = messages.list_chat(session.id)

    assert loaded[0].tool_calls[0]["function"]["arguments"] == {"input": "a\\u0000b"}
    assert loaded[1].content == "result with \\u0000 byte"


def test_tool_effect_round_trip_with_nul_bytes(tmp_path: Path) -> None:
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            session = SessionRepository(connection).create("nul effect")
            effects = ToolEffectRepository(connection)
            intent = ToolEffectRecord(
                idempotency_key="nul-effect-1",
                session_id=str(session.id),
                call_id="call-1",
                agent_id="agent-1",
                tool_name="target_request",
                effect="target_network",
                arguments_digest="sha256:" + "0" * 64,
                arguments_payload={"input": "payload\x00injection"},
                state=ToolEffectState.INTENT,
            )

            stored, created = effects.begin(intent)
            assert created
            assert stored.arguments_payload == {"input": "payload\\u0000injection"}

            finished = effects.finish(
                stored.model_copy(
                    update={
                        "state": ToolEffectState.ACKNOWLEDGED,
                        "result_content": "observed\x00output",
                        "result_is_error": False,
                        "result_metadata": {"head": "a\x00b"},
                    }
                )
            )

    assert finished.result_content == "observed\\u0000output"
    assert finished.result_metadata["head"] == "a\\u0000b"
