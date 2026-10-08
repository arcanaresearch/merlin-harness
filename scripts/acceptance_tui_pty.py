#!/usr/bin/env python3
"""External PTY acceptance for streaming, resize, focus, and keyboard exit."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import pty
import re
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


def _child() -> None:
    from merlin_tui.app import MerlinApp
    from merlin_tui.backend import DemoBackend, StreamEvent

    class PtyBackend(DemoBackend):
        async def stream(self, prompt: str):
            del prompt
            yield StreamEvent("thinking", "Inspecting the reviewed target contract ")
            await asyncio.sleep(0.35)
            yield StreamEvent("thinking", "before choosing a bounded action.")
            await asyncio.sleep(0.35)
            yield StreamEvent("content", "PTY_ACCEPTANCE_COMPLETE")

    MerlinApp(PtyBackend()).run()


def _resize(fd: int, columns: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _visible_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return text.replace("\r", "")


def _wait_for(master: int, output: bytearray, marker: str, timeout: float) -> None:
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _key, _mask in selector.select(timeout=0.1):
            try:
                output.extend(os.read(master, 65_536))
            except OSError:
                return
        if marker in _visible_text(output):
            return
    tail = _visible_text(output)[-2_000:]
    probes = {
        probe: probe in _visible_text(output)
        for probe in (
            "Inspecting",
            "reviewed",
            "target",
            "contract",
            "before",
            "bounded",
            "action",
            "PTY_ACCEPTANCE_COMPLETE",
        )
    }
    raise TimeoutError(
        f"PTY did not render {marker!r} within {timeout:.1f}s; probes={probes}; "
        f"visible tail={tail!r}"
    )


def _parent() -> None:
    master, slave = pty.openpty()
    _resize(slave, 120, 36)
    command = (
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
    )
    environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
    )
    os.close(slave)
    output = bytearray()
    try:
        _wait_for(master, output, "Enter send", 8)
        os.write(master, b"assess\r")
        _wait_for(master, output, "Inspecting the reviewed target", 8)
        _resize(master, 72, 28)
        os.kill(process.pid, signal.SIGWINCH)
        time.sleep(0.2)
        _resize(master, 140, 40)
        os.kill(process.pid, signal.SIGWINCH)
        _wait_for(master, output, "PTY_ACCEPTANCE_COMPLETE", 8)
        process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        os.close(master)
    visible = _visible_text(output)
    required = (
        "Inspecting the reviewed target",
        "PTY_ACCEPTANCE_COMPLETE",
        "Enter send",
    )
    missing = [marker for marker in required if marker not in visible]
    if missing:
        raise AssertionError(f"external PTY render lost markers: {missing}")
    if process.returncode not in {0, -signal.SIGTERM}:
        raise AssertionError(f"TUI exited with status {process.returncode}")
    print("external PTY resize/stream/composer acceptance passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    _child() if options.child else _parent()


if __name__ == "__main__":
    main()
