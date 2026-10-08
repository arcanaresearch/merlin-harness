import os

from merlin_tui.client_state import ClientState, TranscriptEntry


def test_client_state_restores_bounded_draft_and_transcript_with_private_permissions(
    tmp_path,
) -> None:
    path = tmp_path / "client.json"
    state = ClientState(path, history_limit=2)

    state.save(
        "unfinished request",
        (
            TranscriptEntry("content", "old"),
            TranscriptEntry("user", "question"),
            TranscriptEntry("content", "answer"),
        ),
    )

    draft, transcript = state.load()
    assert draft == "unfinished request"
    assert transcript == (
        TranscriptEntry("user", "question"),
        TranscriptEntry("content", "answer"),
    )
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_client_state_corruption_is_non_fatal(tmp_path) -> None:
    path = tmp_path / "client.json"
    path.write_text("not json")

    assert ClientState(path).load() == ("", ())
