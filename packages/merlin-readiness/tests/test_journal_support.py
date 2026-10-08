import json
import stat
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from merlin_readiness import Doctor, OperationalJournal, ReadinessService, ResumeOrchestrator


class Store:
    async def session_exists(self, session_id: str) -> bool:
        return False

    async def load_messages(self, session_id: str):
        return []


def test_operational_journal_is_owner_only_sanitized_and_retained(tmp_path) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    journal = OperationalJournal(
        tmp_path / "logs" / "operational.jsonl",
        max_entries=100,
        max_age_days=30,
        clock=lambda: now,
    )
    journal.path.parent.mkdir(parents=True)
    journal.path.write_text(
        json.dumps(
            {
                "occurred_at": (now - timedelta(days=31)).isoformat(),
                "level": "info",
                "message": "expired",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    journal.append("error", "Authorization: Bearer secret-value", {"api_key": "secret-value"})

    rows = journal.list()
    rendered = json.dumps(rows)
    assert len(rows) == 1
    assert "expired" not in rendered
    assert "secret-value" not in rendered
    assert "[redacted]" in rendered
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_support_bundle_is_bounded_sanitized_and_excludes_transcripts(tmp_path) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    service = ReadinessService(
        doctor=Doctor((("provider", lambda: "Bearer secret-value"),)),
        resume=ResumeOrchestrator(Store()),
        config={"api_key": "secret-value", "model": "safe"},
        logs=lambda **_: [
            {
                "occurred_at": now.isoformat(),
                "level": "error",
                "message": "Bearer secret-value",
            }
        ],
        history=lambda session_id, **_: [
            {
                "occurred_at": now.isoformat(),
                "kind": "turn",
                "summary": "completed",
                "sequence": 1,
                "payload": {"transcript": "raw target evidence", "token": "secret-value"},
            }
        ],
    )

    output = await service.support_bundle(str(tmp_path / "support"), "session-1")
    assert output.endswith("support.zip")
    assert stat.S_IMODE((tmp_path / "support.zip").stat().st_mode) == 0o600
    with zipfile.ZipFile(output) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json",
            "doctor.json",
            "config.json",
            "logs.json",
            "history.json",
        }
        rendered = "".join(bundle.read(name).decode() for name in bundle.namelist())
        history = bundle.read("history.json").decode()
    assert "secret-value" not in rendered
    assert "raw target evidence" not in rendered
    assert "transcript" not in history
    assert "no transcripts" in rendered
    with pytest.raises(FileExistsError):
        await service.support_bundle(output, "session-1")
