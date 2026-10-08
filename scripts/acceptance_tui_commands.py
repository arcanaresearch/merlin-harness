#!/usr/bin/env python3
"""Keyboard-first acceptance matrix for every documented TUI command."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from merlin_tui.app import COMMANDS, MerlinApp
from merlin_tui.local_control import LocalControlClient


@dataclass(frozen=True, slots=True)
class Result:
    command: str
    submitted: str
    outcome: str
    detail: str


def safe_command(command: str) -> str:
    replacements = (
        ("NAME COMMAND ARGS…", "acceptance-mcp /usr/bin/false"),
        ("BUNDLE RECORD REASON…", "missing-bundle missing-record acceptance-reason"),
        ("BUNDLE RECORD…", "missing-bundle missing-record"),
        ("ID REASON…", "missing-id acceptance-reason"),
        ("ID TEXT…", "missing-id acceptance-message"),
        ("ARGS…", "https://example.test"),
        ("[ID|all]", "all"),
        ("NAME", "missing-name"),
        ("DIGEST", "sha256:missing"),
        ("CONTEXT", "missing-context"),
        ("SESSION_ID", "missing-session"),
        ("LIMIT", "1"),
        ("PATH", "/tmp/merlin-command-acceptance.json"),
        ("KIND", "profile"),
        ("ID", "missing-id"),
    )
    for placeholder, value in replacements:
        command = command.replace(placeholder, value)
    return command


async def exercise(command: str) -> Result:
    submitted = safe_command(command)
    app = MerlinApp(LocalControlClient())
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            composer = app.query_one("#composer")
            composer.text = submitted
            await pilot.press("enter")
            await pilot.pause(0.25)
            if command == "/quit":
                if app.is_running:
                    return Result(command, submitted, "FAIL", "keyboard /quit did not exit")
                return Result(command, submitted, "PASS", "exited cleanly")

            if app.screen is not app:
                await pilot.press("escape")
                await pilot.pause()
            stream_task = app._stream_task
            if stream_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(stream_task), timeout=5.0)
                except TimeoutError:
                    await app.action_interrupt()
                    return Result(command, submitted, "FAIL", "command did not settle in 5s")
            if not app.is_running:
                return Result(command, submitted, "FAIL", "command exited the TUI")
            if composer.disabled:
                return Result(command, submitted, "FAIL", "composer remained disabled")
            if not composer.has_focus:
                composer.focus()
                await pilot.pause()
                if not composer.has_focus:
                    return Result(command, submitted, "FAIL", "focus did not return")
            transcript = "\n".join(line.text for line in app.query_one("#transcript").lines)
            if "Unknown command" in transcript or "Provider failed" in transcript:
                return Result(command, submitted, "FAIL", "command escaped local routing")
            if "Traceback" in transcript or "GeneratorExit" in transcript:
                return Result(command, submitted, "FAIL", "internal exception reached the UI")
            detail = "completed"
            if "usage:" in transcript.lower() or "not found" in transcript.lower():
                detail = "validated disposable placeholder"
            return Result(command, submitted, "PASS", detail)
    except Exception as exc:
        return Result(command, submitted, "FAIL", f"{type(exc).__name__}: {exc}")


async def run_all() -> list[Result]:
    results = []
    for command in COMMANDS:
        results.append(await exercise(command))
    return results


def render(results: list[Result]) -> str:
    passed = sum(result.outcome == "PASS" for result in results)
    lines = [
        "# TUI command acceptance matrix",
        "",
        f"Result: {passed}/{len(results)} passed",
        "",
        "The matrix uses keyboard submission through the real TUI and local-control daemon. "
        "Placeholder identifiers target disposable or intentionally missing records.",
        "",
        "| Command | Submitted form | Result | Detail |",
        "|---|---|---:|---|",
    ]
    for result in results:
        values = (
            result.command.replace("|", "\\|"),
            result.submitted.replace("|", "\\|"),
            result.outcome,
            result.detail.replace("|", "\\|"),
        )
        lines.append(f"| `{values[0]}` | `{values[1]}` | {values[2]} | {values[3]} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    results = asyncio.run(run_all())
    report = render(results)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(report)
    print(report)
    if any(result.outcome != "PASS" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
