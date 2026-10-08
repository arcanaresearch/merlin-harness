from __future__ import annotations

import shlex

from merlin_contracts import AppCommand

COMMANDS = (
    "help",
    "setup",
    "model",
    "new",
    "sessions",
    "resume",
    "branch",
    "rewind",
    "restore",
    "status",
    "plugins",
    "doctor",
    "logs",
    "history",
    "support",
    "mode",
    "permissions",
    "targets",
    "skills",
    "mcp",
    "agents",
    "tasks",
    "agent",
    "findings",
    "report",
    "memory",
    "browser",
    "promotion",
    "clear",
    "interrupt",
    "quit",
)


class CommandError(ValueError):
    pass


def parse_command(text: str) -> AppCommand | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    try:
        parts = shlex.split(stripped[1:])
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    if not parts:
        raise CommandError("empty command")
    name = parts[0].lower()
    if name not in COMMANDS:
        raise CommandError(f"unknown command: /{name}")
    return AppCommand(name=name, arguments=parts[1:])


def help_text() -> str:
    return "Commands: " + ", ".join(f"/{name}" for name in COMMANDS)
