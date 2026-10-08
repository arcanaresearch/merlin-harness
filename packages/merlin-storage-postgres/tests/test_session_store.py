import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from merlin_contracts import AppEvent, ChatMessage, SessionRecord
from merlin_storage_postgres import PostgresSessionStore

NOW = datetime.now(UTC)


class Sessions:
    records = [SessionRecord(id="s1", created_at=NOW, updated_at=NOW)]

    def create(self):
        return self.records[0]

    def list(self, *, limit=50, offset=0, query=""):
        matches = [record for record in self.records if query.casefold() in record.id.casefold()]
        return matches[offset : offset + limit]

    def count(self, *, query=""):
        return len([record for record in self.records if query.casefold() in record.id.casefold()])

    def get(self, session_id):
        return self.records[0] if session_id == "s1" else None

    def projection_head(self, session_id):
        return 1


class Messages:
    def __init__(self):
        self.items = []

    def append(self, session_id, role, content, **details):
        self.items.append((session_id, role, content, details))

    def list_chat(self, session_id):
        return [
            ChatMessage(role=role, content=content, **details)
            for _, role, content, details in self.items
        ]

    def clear(self, session_id):
        self.items.clear()

    def provenance(self, session_id):
        return [
            {
                "sequence": 1,
                "role": "tool",
                "message_id": "message-1",
                "source_message_id": "source-1",
            }
        ]


class Compactions:
    def __init__(self):
        self.lineage_requests = []

    def list(self, session_id):
        return []

    def list_lineage(self, session_id):
        self.lineage_requests.append(session_id)
        return []


class Events:
    def __init__(self):
        self.items = []

    def append(self, session_id, event_type, payload):
        self.items.append((session_id, event_type, payload))


def test_async_session_store_adapter() -> None:
    asyncio.run(_exercise_adapter())


async def _exercise_adapter() -> None:
    messages, events = Messages(), Events()
    compactions = Compactions()
    store = PostgresSessionStore(
        SimpleNamespace(),
        sessions=Sessions(),
        messages=messages,
        events=events,
        compactions=compactions,
    )

    assert await store.create_session(model="ignored") == "s1"
    assert await store.session_exists("s1")
    assert not await store.session_exists("missing")
    assert (await store.list_sessions())[0]["id"] == "s1"
    assert (await store.list_sessions(limit=1, offset=0, query="s1"))[0]["id"] == "s1"
    assert await store.count_sessions(query="missing") == 0

    message = ChatMessage(role="tool", content="done", name="reader", tool_call_id="call-1")
    await store.append_message("s1", message)
    assert list(await store.load_messages("s1")) == [message]
    context = await store.context_provenance("s1")
    assert context["projection_head"] == 1
    assert context["transcript_head"] == 1
    assert context["messages"][0]["source_message_id"] == "source-1"
    assert compactions.lineage_requests == ["s1"]

    event = AppEvent(type="tool", payload={"state": "completed"}, session_id="s1", occurred_at=NOW)
    await store.append_event("s1", event)
    assert events.items == [("s1", "tool", {"state": "completed"})]

    await store.clear_session("s1")
    assert list(await store.load_messages("s1")) == []
