import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from merlin_readiness import (
    Doctor,
    HistoryEntry,
    LogEntry,
    ResourceCloser,
    ResumeOrchestrator,
    audit_packaging,
    export_config,
)


@pytest.mark.asyncio
async def test_doctor_contains_failures_and_sanitizes_details() -> None:
    def failed() -> None:
        raise RuntimeError("postgresql://user:password@localhost/merlin")

    report = await Doctor((("config", lambda: "loaded"), ("postgresql", failed))).run()

    assert not report.healthy
    assert [(item.name, item.status) for item in report.checks] == [
        ("config", "ok"),
        ("postgresql", "failed"),
    ]
    assert "password" not in report.checks[1].detail
    assert report.checks[1].detail == "postgresql://localhost/merlin"


def test_logs_history_and_config_export_never_expose_secrets() -> None:
    now = datetime.now(UTC)
    log = LogEntry.create(now, "error", "Bearer abc123 failed", {"api_key": "abc123"})
    history = HistoryEntry.create(now, "setup", "saved", {"password": "hunter2"})
    exported = export_config(
        {
            "provider": {"base_url": "https://api.example/v1", "api_key": "abc123"},
            "database": {"url": "postgresql://lucas:hunter2@localhost/merlin"},
        }
    )

    assert log.message == "Bearer [redacted] failed"
    assert log.context["api_key"] == "[redacted]"
    assert history.metadata["password"] == "[redacted]"
    assert "abc123" not in exported
    assert "hunter2" not in exported
    assert json.loads(exported)["database"]["url"] == "postgresql://localhost/merlin"


class FakeStore:
    async def session_exists(self, session_id: str) -> bool:
        return session_id == "known"

    async def load_messages(self, session_id: str) -> list[str]:
        return ["one", "two"]


@pytest.mark.asyncio
async def test_resume_validates_recovers_then_loads() -> None:
    calls = []

    async def recover(session_id: str) -> int:
        calls.append(session_id)
        return 2

    result = await ResumeOrchestrator(FakeStore(), recover).resume("known")

    assert result.messages == ("one", "two")
    assert result.recovered == 2
    assert calls == ["known"]
    with pytest.raises(KeyError):
        await ResumeOrchestrator(FakeStore()).resume("missing")

    class EmptyStore(FakeStore):
        async def load_messages(self, session_id: str) -> list[str]:
            return []

    with pytest.raises(ValueError, match="no conversation"):
        await ResumeOrchestrator(EmptyStore()).resume("known")


@pytest.mark.asyncio
async def test_resource_shutdown_is_reverse_order_idempotent_and_best_effort() -> None:
    calls = []

    class Resource:
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    closer = ResourceCloser(Resource("database"), Resource("connection", fail=True))
    with pytest.raises(ExceptionGroup):
        await closer.close()
    await closer.close()
    assert calls == ["connection", "database"]


def test_app_is_uv_tool_and_wheel_ready() -> None:
    root = Path(__file__).parents[3]
    audit = audit_packaging(root / "apps/merlin-tui/pyproject.toml", root / "uv.lock")

    assert audit.package == "merlin-tui"
    assert audit.script == "merlin_tui.cli:main"
    assert audit.wheel_backend == "hatchling.build"
    assert audit.macos_arm_embedded_postgres
