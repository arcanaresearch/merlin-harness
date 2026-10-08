from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import SkillCatalog
from .errors import SkillNotFoundError


class SessionSkillService:
    """Expose inert skill instructions with session-scoped enablement."""

    def __init__(self, catalog: SkillCatalog, state_path: Path | None = None) -> None:
        self._catalog = catalog
        self._enabled: dict[str, set[str]] = defaultdict(set)
        self._state_path = Path(state_path) if state_path else None
        self._load_state()

    async def list_skills(self, session_id: str) -> Sequence[Mapping[str, Any]]:
        return [
            asdict(info) | {"directory": str(info.directory), "enabled": info.name in enabled}
            for info in self._catalog.list()
            for enabled in (self._enabled[session_id],)
        ]

    async def inspect_skill(self, name: str) -> Mapping[str, Any]:
        skill = self._catalog.load(name)
        return {
            "name": skill.name,
            "description": skill.description,
            "content": skill.content,
            "directory": str(skill.directory),
        }

    async def enable(self, session_id: str, name: str) -> Mapping[str, Any]:
        skill = self._catalog.load(name)
        self._enabled[session_id].add(name)
        self._save_state()
        return {"name": skill.name, "version": skill.version, "enabled": True}

    async def disable(self, session_id: str, name: str) -> Mapping[str, Any]:
        skill = self._catalog.load(name)
        self._enabled[session_id].discard(name)
        self._save_state()
        return {"name": skill.name, "enabled": False}

    async def instructions_for(self, session_id: str, user_text: str) -> Sequence[str]:
        names = list(sorted(self._enabled[session_id]))
        for name in self._catalog.extract_mentions(user_text):
            if name in names:
                continue
            try:
                self._catalog.load(name)
            except SkillNotFoundError:
                continue
            names.append(name)
        return [self._catalog.load(name).content for name in names]

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot load skill enablement: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError("skill enablement state must be an object")
        for session_id, selections in raw.items():
            if not isinstance(session_id, str) or not isinstance(selections, dict):
                raise ValueError("invalid skill enablement state")
            for name, version in selections.items():
                skill = self._catalog.load(name)
                if version != skill.version:
                    raise ValueError(
                        f"enabled skill {name!r} changed from {version!r} to {skill.version!r}; "
                        "review and enable it again"
                    )
                self._enabled[session_id].add(name)

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            session: {name: self._catalog.load(name).version for name in sorted(names)}
            for session, names in self._enabled.items()
            if names
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".skills-", dir=self._state_path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
            temporary = ""
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
