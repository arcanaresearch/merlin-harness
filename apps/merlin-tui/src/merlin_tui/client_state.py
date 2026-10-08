from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    kind: str
    text: str


class ClientState:
    """Small, client-local UI state; durable campaign state remains daemon-owned."""

    def __init__(self, path: Path | None = None, *, history_limit: int = 500) -> None:
        client_id = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", os.environ.get("MERLIN_CLIENT_ID", "tui")).strip("-")
            or "tui"
        )
        self.path = path or Path.home() / ".merlin" / "clients" / f"{client_id}.json"
        self.history_limit = history_limit

    def load(self) -> tuple[str, tuple[TranscriptEntry, ...]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            draft = str(value.get("draft", ""))
            transcript = tuple(
                TranscriptEntry(str(item["kind"]), str(item["text"]))
                for item in value.get("transcript", ())[-self.history_limit :]
                if isinstance(item, dict) and "kind" in item and "text" in item
            )
            return draft, transcript
        except (OSError, ValueError, TypeError, KeyError):
            return "", ()

    def save(self, draft: str, transcript: tuple[TranscriptEntry, ...]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {
            "draft": draft,
            "transcript": [
                {"kind": item.kind, "text": item.text} for item in transcript[-self.history_limit :]
            ],
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
