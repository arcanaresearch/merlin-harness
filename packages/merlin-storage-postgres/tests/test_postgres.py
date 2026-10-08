import os

import psycopg
import pytest
from merlin_storage_postgres import (
    CampaignStore,
    EventRepository,
    MessageRepository,
    MigrationRunner,
    SessionRepository,
)
from psycopg.rows import dict_row


@pytest.mark.postgres
def test_repository_round_trip() -> None:
    url = os.environ.get("MERLIN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set MERLIN_TEST_DATABASE_URL for the PostgreSQL boundary test")

    with psycopg.connect(url, row_factory=dict_row) as connection:
        MigrationRunner(connection).apply()
        sessions = SessionRepository(connection)
        messages = MessageRepository(connection)
        events = EventRepository(connection)

        session = sessions.create("integration")
        assert sessions.list(limit=1, offset=0, query=str(session.id))[0].id == session.id
        assert sessions.count(query=str(session.id)) == 1
        campaigns = CampaignStore(connection)
        prepared = campaigns.prepare(
            "campaign/integration",
            str(session.id),
            {"objective": "Assess the reviewed target"},
            "sha256:reviewed",
        )
        assert prepared["status"] == "prepared"
        assert campaigns.get("campaign/integration")["manifest_digest"] == "sha256:reviewed"
        messages.append(session.id, "user", "hello")
        messages.append(session.id, "assistant", "hi")
        first = events.append(session.id, "turn.started", {"turn": 1})
        second = events.append(session.id, "turn.finished", {"turn": 1})

        assert [message.role for message in messages.list(session.id)] == ["user", "assistant"]
        assert [event.sequence for event in events.list(session.id)] == [
            first.sequence,
            second.sequence,
        ]
        assert events.list(session.id, after=first.sequence) == [second]


def test_default_data_dir_adopts_legacy_cluster_only_when_initialized(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from merlin_storage_postgres.database import DatabaseConfig

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    assert DatabaseConfig().data_dir == home / ".merlin" / "postgres"

    legacy = home / ".redpen-python" / "postgres"
    legacy.mkdir(parents=True)
    # An empty legacy directory is not an initialized cluster.
    assert DatabaseConfig().data_dir == home / ".merlin" / "postgres"
    (legacy / "PG_VERSION").write_text("18\n", encoding="utf-8")
    assert DatabaseConfig().data_dir == legacy

    preferred = home / ".merlin" / "postgres"
    preferred.mkdir(parents=True)
    (preferred / "PG_VERSION").write_text("18\n", encoding="utf-8")
    assert DatabaseConfig().data_dir == preferred
