from datetime import UTC, datetime

import pytest
from merlin_readiness import Doctor, LogEntry, ReadinessService, ResumeOrchestrator


class Store:
    async def session_exists(self, session_id: str) -> bool:
        return session_id == "session-1"

    async def load_messages(self, session_id: str):
        return [{"role": "assistant", "content": "ready"}]


@pytest.mark.asyncio
async def test_executable_readiness_projection_is_restart_safe_and_sanitized() -> None:
    async def recover(session_id: str) -> int:
        assert session_id == "session-1"
        return 2

    now = datetime.now(UTC)
    service = ReadinessService(
        doctor=Doctor(
            [
                ("postgres", lambda: "postgresql://user:password@localhost/merlin"),
                ("provider", lambda: "Bearer production-secret"),
            ]
        ),
        resume=ResumeOrchestrator(Store(), recover),
        config={
            "provider": {"model": "test", "api_key": "production-secret"},
            "database": {"url": "postgresql://localhost/merlin"},
        },
        logs=lambda **_: [LogEntry.create(now, "info", "Authorization: Bearer production-secret")],
        history=lambda session_id, **_: [
            {"kind": "resume", "session_id": session_id, "token": "production-secret"}
        ],
        schedules=lambda session_id: [
            {
                "id": "schedule-1",
                "session_id": session_id,
                "graph": {"target": "https://plunder.me/policy-refund-trap/chat?levelId=1"},
            }
        ],
        sessions=lambda **page: [
            {"id": "session-2", "title": "Second", "page": page},
        ],
        session_count=lambda **_: 7,
        context=lambda session_id: {
            "session_id": session_id,
            "projection_head": 2,
            "transcript_head": 4,
            "messages": [{"sequence": 2, "role": "assistant"}],
            "compactions": [{"source_digest": "a" * 64}],
            "secret": "production-secret",
        },
    )

    projection = await service.resume("session-1")
    rendered = str(
        {
            "doctor": await service.doctor(),
            "logs": await service.logs(),
            "history": await service.history("session-1"),
            "config": await service.export_config(),
            "resume": projection,
        }
    )
    assert projection["recovered"] == 2
    assert projection["schedules"][0]["id"] == "schedule-1"
    assert projection["schedules"][0]["graph"]["target"].endswith("?levelId=1")
    assert projection["messages"][0]["content"] == "ready"
    assert "production-secret" not in rendered
    assert "password" not in rendered

    page = await service.session_page(limit=1, offset=1, query="Second")
    assert page["total"] == 7
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert page["items"][0]["id"] == "session-2"
    context = await service.context("session-1")
    assert context["projection_head"] == 2
    assert context["messages"][0]["sequence"] == 2
    assert context["secret"] == "[redacted]"
