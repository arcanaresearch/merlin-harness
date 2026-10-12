from merlin_storage_postgres import Database, DatabaseConfig, MigrationRunner, SessionRepository
from merlin_storage_postgres.repositories import MessageRepository


def test_chat_history_cursor_pages_stay_stable_across_inserts_and_sessions(tmp_path):
    with Database(DatabaseConfig(data_dir=tmp_path / "postgres")) as database:
        with database.connect() as connection:
            MigrationRunner(connection).apply()
            sessions, messages = SessionRepository(connection), MessageRepository(connection)
            first, second = sessions.create("History"), sessions.create("Other")
            expected = []
            for index in range(123):
                row = messages.append(
                    first.id, "user" if index % 2 == 0 else "assistant", f"Message {index}"
                )
                expected.append(row.sequence)
            messages.append(second.id, "user", "Other session")
            page = messages.history_page(first.id)
            assert len(page["messages"]) == 50 and page["has_more"]
            assert [row["sequence"] for row in page["messages"]] == expected[-50:]
            messages.append(first.id, "assistant", "Inserted after first page")
            older = messages.history_page(first.id, before=page["before"])
            oldest = messages.history_page(first.id, before=older["before"])
            assert [
                row["sequence"] for row in oldest["messages"] + older["messages"] + page["messages"]
            ] == expected
            assert not oldest["has_more"]
            assert all(row["content"] != "Other session" for row in page["messages"])
            prior_turn = messages.history_page(first.id, exclude_current_turn=True)
            assert prior_turn["messages"][-1]["sequence"] == expected[-2]
            assert not messages.history_page(first.id, before=expected[0])["messages"]
            connection.execute(
                "UPDATE merlin_storage.sessions SET projection_head = %s WHERE id = %s",
                (expected[60], first.id),
            )
            connection.commit()
            rewound = messages.history_page(first.id)
            assert rewound["messages"][-1]["sequence"] == expected[60]
            prior = messages.history_page(first.id, exclude_current_turn=True)
            assert prior["messages"][-1]["sequence"] == expected[59]
