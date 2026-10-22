#!/usr/bin/env python3
"""PTY acceptance of the interactive TUI campaign journey on the packaged binary.

Drives the real packaged release (daemon + embedded pg0 + bundled Chromium)
over a PTY with a scripted local mock LLM (``scripts/mock_llm_server.py``) and
the seeded fixture target (``merlin_assessments.FixtureTargetServer``), proving
the operator journey end-to-end without a human:

1. a campaign objective is submitted against the local fixture target;
2. the campaign review appears; API execution is approved and proven against
   the fixture target's own request log (httpx user agent, no browser);
3. the same for Hybrid mode (browser-context API transport, Mozilla UA),
   selected through the TUI review's own execution-mode choice;
4. specialist composition policy (opinionated/autonomous) is chosen through
   the TUI's composition control and lands in the sealed manifest/review;
5. with ``permission_mode=auto`` no permission prompt appears for
   observational effects (workspace_read/provider_network/target_read);
6. on completion the TUI automatically presents the exported report path;
7. terminal close pauses the campaign; explicit same-session resume completes
   it after relaunch without duplicate execution.

Browser-only execution is attempted honestly and asserted as a product
finding (see FINDINGS in the printed report): the reviewed flow cannot seal a
browser adapter for an API-backed chat target, so only api/hybrid modes are
reachable. Nothing is faked.

Run::

    uv run python scripts/acceptance_tui_campaign_pty.py \
        --payload target/releases/merlin-0.1.0-darwin-arm64 \
        --workdir target/pty-campaign-uat
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import pty
import re
import selectors
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_llm_server import make_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

RESULTS: list[tuple[str, bool, str]] = []

# This is a regression floor, not a product limit. The exact Recommended
# frontier is derived by the packaged product from the reviewed target surface.
# Requiring a broad frontier here prevents the old 1–2 technique collapse while
# allowing the catalogue to grow without teaching the acceptance driver a
# second copy of production applicability rules.
MINIMUM_MEANINGFUL_RECOMMENDED_TECHNIQUES = 20


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def _visible_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return text.replace("\r", "")


def _resize(fd: int, columns: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


class FixtureTarget:
    """In-process fixture target server on its own event-loop thread."""

    def __init__(self) -> None:
        from merlin_assessments import FixtureTargetServer

        self.server = FixtureTargetServer(match_non_recon_message=True)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server._server is not None:  # noqa: SLF001
                break
            time.sleep(0.05)
        self.origin = self.server.origin
        self._marks: list[tuple[int, str]] = []

    def mark_attack_phase(self, phase: str) -> None:
        """Tag all fixture invocations from this point onward with `phase`."""
        self._marks.append((len(self.server.http_log), phase))

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.__aenter__())
        self.loop.run_forever()

    def invocations(self, operation: str) -> list[dict]:
        out = []
        for index, entry in enumerate(self.server.http_log):
            if entry.get("operation") != operation:
                continue
            tagged = None
            for boundary, phase in self._marks:
                if index >= boundary:
                    tagged = phase
            out.append({**entry, "attack_phase": tagged})
        return out

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


class Tui:
    """One PTY-attached packaged-TUI process."""

    def __init__(self, binary: Path, home: Path, workspace: Path) -> None:
        self.binary, self.home, self.workspace = binary, home, workspace
        self.output = bytearray()
        self.permission_effects: list[str] = []
        self.keylog: list[tuple[float, bytes]] = []
        self._fresh_cache: dict[int, tuple[int, str]] = {}
        self._scan_offset = 0
        self._last_approval: tuple[str, float] = ("", 0.0)
        self._permission_open = False
        master, slave = pty.openpty()
        _resize(slave, 140, 40)
        environment = dict(
            os.environ,
            HOME=str(home),
            TERM="xterm-256color",
            COLORTERM="truecolor",
        )
        self.process = subprocess.Popen(
            [str(binary)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            cwd=workspace,
            env=environment,
        )
        os.close(slave)
        self.master = master

    def visible(self) -> str:
        return _visible_text(self.output)

    def fresh(self, start: int) -> str:
        """Text that arrived strictly after byte offset ``start``."""
        cached_at, cached_text = self._fresh_cache.get(start, (start, ""))
        current = len(self.output)
        if cached_at < current:
            cached_text += _visible_text(self.output[cached_at:current])
            self._fresh_cache[start] = (current, cached_text)
        return cached_text

    def _pump(self, timeout: float = 0.1) -> bool:
        selector = selectors.DefaultSelector()
        selector.register(self.master, selectors.EVENT_READ)
        try:
            events = selector.select(timeout=timeout)
        finally:
            selector.close()
        if not events:
            return True
        try:
            self.output.extend(os.read(self.master, 65_536))
        except OSError:
            return False
        return True

    def wait(self, marker: str, timeout: float, *, forbid: tuple[str, ...] = ()) -> str:
        # Only output arriving AFTER this wait starts may satisfy it; earlier
        # scrollback belongs to previous phases/TUI instances and is stale.
        start = len(self.output)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pump(0.1):
                break
            self._scan_permissions()
            visible = self.fresh(start)
            if marker in visible:
                return visible
            hit = next((item for item in forbid if item in visible), None)
            if hit is not None:
                raise AssertionError(
                    f"forbidden text {hit!r} rendered while waiting for {marker!r}; "
                    f"tail={visible[-1500:]!r}"
                )
        raise TimeoutError(
            f"PTY did not render {marker!r} within {timeout:.1f}s; tail={self.visible()[-3000:]!r}"
        )

    def wait_idle(self, timeout: float) -> None:
        """Wait until no backend turn is streaming.

        While a turn streams, the activity indicator repaints the
        "Esc to interrupt" line every 0.12s; when the byte stream stops
        gaining occurrences for a short stability window, the TUI is idle
        and keystrokes will land.
        """
        start = len(self.output)
        deadline = time.monotonic() + timeout
        last_count = -1
        stable_since: float | None = None
        while time.monotonic() < deadline:
            self._pump(0.25)
            self._scan_permissions()
            count = self.fresh(start).count("Esc to interrupt")
            if count != last_count:
                last_count = count
                stable_since = None
                continue
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 1.0:
                return
        raise TimeoutError(f"TUI did not become idle within {timeout:.1f}s")

    def wait_count(self, marker: str, count: int, timeout: float) -> str:
        start = len(self.output)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._pump(0.1):
                break
            self._scan_permissions()
            visible = self.fresh(start)
            if visible.count(marker) >= count:
                return visible
        raise TimeoutError(
            f"PTY rendered {marker!r} fewer than {count} times within {timeout:.1f}s; "
            f"tail={self.visible()[-2000:]!r}"
        )

    def wait_gone(self, marker: str, timeout: float) -> None:
        start = len(self.output)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump(0.1)
            if marker not in self.fresh(start):
                return
        raise TimeoutError(f"{marker!r} still rendered after {timeout:.1f}s")

    def _scan_permissions(self) -> None:
        """Approve mutation permission prompts; record every prompted effect.

        Support both the older prompt, which rendered explicit effect/tool
        labels, and the current compact prompt, which follows the transcript's
        ``Tool · <name> started`` line. Each byte is scanned exactly once, and
        only up to the last ANSI escape boundary, so a prompt split across reads
        is deferred instead of being matched truncated; a short same-signature
        suppression guards against a double approval keystroke leaking into the
        composer.
        """
        end = len(self.output)
        if end <= self._scan_offset:
            return
        data = bytes(self.output[self._scan_offset : end])
        limit = data.rfind(b"\x1b[")
        if limit <= 0:
            return
        self._scan_offset += limit
        visible = _visible_text(data[:limit])
        if self._permission_open and re.search(
            r"Tool · [\w-]+ (?:completed|failed)", visible
        ):
            self._permission_open = False
        prompts = list(re.finditer(r"Permission required", visible))
        labelled = list(re.finditer(r"Effect: (\w+) · Tool: ([\w-]+)", visible))
        for match in prompts:
            if self._permission_open:
                continue
            explicit = next(
                (item for item in reversed(labelled) if item.start() < match.start()),
                None,
            )
            if explicit is not None:
                effect, tool = explicit.group(1), explicit.group(2)
            else:
                transcript = self.visible()
                started = list(
                    re.finditer(r"Tool · ([\w-]+) started", transcript)
                )
                tool = started[-1].group(1) if started else "unknown"
                effect = {
                    "browser_fill": "target_network",
                    "browser_click": "target_network",
                    "browser_press": "target_network",
                    "browser_chat_exchange": "target_network",
                    "browser_compile_api_handoff": "harness_state",
                    "browser_record_api_handoff": "harness_state",
                    "assessment_prepare": "harness_state",
                    "campaign_configure": "workspace_write",
                    "browser_open_tab": "target_read",
                    "browser_snapshot": "target_read",
                    "browser_network_events": "target_read",
                }.get(tool, "unknown")
            signature = f"{effect}:{tool}"
            now = time.monotonic()
            if signature == self._last_approval[0] and now - self._last_approval[1] < 2.0:
                continue
            self._last_approval = (signature, now)
            self.permission_effects.append(signature)
            self._permission_open = True
            print(f"  …permission prompt auto-approved: {effect} · {tool}", flush=True)
            self._write(b"1")

    def _write(self, data: bytes) -> None:
        """Pace keystrokes and keep the PTY drained between them.

        The packaged TUI drops keystrokes that arrive in one burst while its
        output is not being drained (observed against Textual 1.0 over a
        macOS PTY); human-speed delivery with pumping is reliable.
        """
        for index in range(len(data)):
            os.write(self.master, data[index : index + 1])
            self.keylog.append((time.monotonic(), data[index : index + 1]))
            self._pump(0.02)
            self._scan_permissions()

    def send(self, text: str) -> None:
        self._write(text.encode())
        self._write(b"\r")

    def stop(self) -> None:
        if self.process.poll() is None:
            # Exercise Merlin's advertised Ctrl+Z clean-exit path so the TUI
            # releases its foreground lease before the acceptance driver
            # reconnects. SIGTERM bypasses Textual teardown and can leave the
            # packaged runtime child defunct, which tests a crash rather than
            # a user closing the TUI.
            self._write(b"\x1a")
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass


def _cli(binary: Path, home: Path, workspace: Path, *args: str) -> str:
    result = subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=workspace,
        env=dict(os.environ, HOME=str(home)),
    )
    if result.returncode != 0:
        raise AssertionError(
            f"merlin {' '.join(args)} exited {result.returncode}: "
            f"{result.stdout[-800:]} {result.stderr[-800:]}"
        )
    return result.stdout


def _seed_home(home: Path, llm_base_url: str) -> None:
    config_dir = home / ".merlin"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    config_toml = config_dir / "config.toml"
    config_toml.write_text(
        'permission_mode = "auto"\n'
        "\n"
        "[provider]\n"
        f'base_url = "{llm_base_url}"\n'
        'model = "mock-model"\n',
        encoding="utf-8",
    )
    config_toml.chmod(stat.S_IRUSR | stat.S_IWUSR)
    credentials = config_dir / "credentials.json"
    credentials.write_text(
        json.dumps({"api_key": "mock-key", "target_pty-target": "fixture-key"}),
        encoding="utf-8",
    )
    credentials.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _settle(tui: Tui, boot_timeout: float, *, resume_existing: bool = False) -> None:
    """Wait until the TUI is truly interactive (daemon attached).

    ``ready · /help`` is static compose-time text; keystrokes only land
    reliably once the status line reports the attached daemon. A startup
    session gate (Resume/New) may appear asynchronously when the daemon
    holds prior sessions; choose the requested explicit lifecycle action.
    """
    tui.wait("ready · /help", boot_timeout)
    deadline = time.monotonic() + boot_timeout
    gate_answered = False
    while time.monotonic() < deadline:
        tui._pump(0.2)  # noqa: SLF001
        visible = tui.visible()
        if not gate_answered and "New session" in visible:
            tui.send("1" if resume_existing else "2")
            gate_answered = True
            print(
                "  …startup session gate answered: "
                + ("resume" if resume_existing else "new session"),
                flush=True,
            )
            continue
        if "daemon attached" in visible:
            return
    raise TimeoutError(
        f"TUI did not attach to the daemon within {boot_timeout:.1f}s; "
        f"gate_answered={gate_answered}; screen={tui.visible()[-3_000:]!r}"
    )


OBJECTIVE = (
    "Execute the reviewed seeded UAT objective against {origin} in one multi-turn "
    "authority-build conversation."
)


def _submit_campaign(tui: Tui, origin: str, token: str, composition: str) -> None:
    tui.send(f"{OBJECTIVE.format(origin=origin)} {token}")
    tui.wait("Exploration / learning", 240)
    tui.wait_idle(180)  # choices are only armed once the turn finishes
    tui.send("1")  # campaign profile: recommended
    tui.wait("Specialist composition", 30)
    tui.send("2" if composition == "autonomous" else "1")


def _wait_review(tui: Tui, timeout: float) -> str:
    """Wait for a freshly rendered campaign review panel.

    The panel's Rich title never reaches the byte stream (RichLog paints it
    without the title text), so the approval hint is the reliable marker.
    """
    return tui.wait("Press Enter to approve", timeout)


def _wait_report_through_coverage_decisions(
    tui: Tui,
    timeout: float,
    *,
    campaign_id: str,
    stall_timeout: float = 150,
    max_coverage_continuations: int = 3,
) -> tuple[str, tuple[str, ...]]:
    """Resolve coverage choices in the attached TUI until it presents a report.

    Starting a standalone ``merlin campaign status`` process while the TUI owns
    the backend can trigger recovery fencing and pause the campaign being
    observed. Drive every required-coverage continuation through the attached
    operator-facing choice screen. Continue through several independently
    reviewed repair plans, then stop if the same unresolved frontier returns;
    that proves the honest completed-with-limitations report path without an
    unbounded target loop.
    """
    start = len(tui.output)
    # This is a safety ceiling, not an activity timeout. Broad packaged
    # campaigns can legitimately exceed the historical seven-minute budget
    # while continuing to finish tasks and advance waves.
    deadline = time.monotonic() + max(timeout, 1_200)
    progress_deadline = time.monotonic() + stall_timeout
    highest_wave = -1
    decisions: list[str] = []
    decision_wave = -1
    choice_scan = start
    handled_request_ids: set[str] = set()
    while time.monotonic() < deadline:
        if not tui._pump(0.2):  # noqa: SLF001
            break
        tui._scan_permissions()  # noqa: SLF001
        # Campaign rendering is append-only and can exceed tens of megabytes.
        # Every marker used below is a current-state marker, so scan a bounded
        # recent window instead of repeatedly regexing the full PTY history.
        fresh = tui.fresh(start)[-250_000:]
        if "Campaign report ready" in fresh:
            return fresh, tuple(decisions)
        # Only the workspace header is authoritative campaign progress. Lead
        # journals and task prose also contain ``Wave N`` references, sometimes
        # to a newer planning wave than the modal currently being answered.
        observed_waves = [
            int(value)
            for value in re.findall(
                r"\bWave (\d+)\s+·\s+[^·\r\n]+\s+·\s+campaign totals below",
                fresh,
            )
        ]
        current_wave = max(observed_waves, default=highest_wave)
        if current_wave > highest_wave:
            highest_wave = current_wave
            progress_deadline = time.monotonic() + stall_timeout
        if len(decisions) >= 128:
            raise TimeoutError(
                "campaign requested more than 128 distinct coverage decisions without completing"
            )
        # Restrict choices to bytes received during this campaign and after
        # the preceding decision. ``Tui.visible()`` is append-only PTY history,
        # so using it here can reuse a modal from a prior campaign. If the next
        # modal arrives before its header, these bytes remain available until
        # the authoritative header parser above observes the wave advance.
        choices = tui.fresh(choice_scan)[-250_000:]
        if not any(
            marker in choices
            for marker in (
                "Replan missing coverage and continue",
                "Continue exhaustive assessment",
            )
        ):
            if time.monotonic() >= progress_deadline:
                raise TimeoutError(
                    "campaign stopped advancing before exporting a report: "
                    f"last_wave={highest_wave}, stall_timeout={stall_timeout}s; "
                    f"tail={tui.visible()[-3000:]!r}"
                )
            continue
        request_ids = re.findall(r"Decision checkpoint:\s*([^\s]+)", choices)
        if not request_ids:
            continue
        request_id = request_ids[-1]
        if request_id in handled_request_ids:
            choice_scan = len(tui.output)
            continue
        action = "stop" if (
            "Continue exhaustive assessment" in choices
            or decisions.count("continue") >= max_coverage_continuations
        ) else "continue"
        # Exercise the modal's numbered operator action directly. Dismissing
        # with Escape can activate the focused "limit" path and prefill the
        # composer, corrupting a command typed immediately afterward.
        acknowledgement_start = len(tui.output)
        # Exclude the modal being dismissed, but retain every byte emitted
        # after the choice begins. The next saturation modal can render
        # before its acknowledgement is observed.
        choice_scan = acknowledgement_start
        tui.send("1" if action == "stop" else "2")
        # The attached local-control transport serializes requests. A campaign
        # progress refresh that began before the modal can finish ahead of the
        # decision, so submitting a valid operator choice is itself progress.
        progress_deadline = time.monotonic() + stall_timeout
        acknowledgement_deadline = time.monotonic() + 60
        acknowledged = False
        while time.monotonic() < acknowledgement_deadline:
            tui._pump(0.1)  # noqa: SLF001
            result = tui.fresh(acknowledgement_start)
            if f"Exhaustive campaign decision recorded: {action}" in result:
                acknowledged = True
                break
            if "Campaign decision failed:" in result:
                break
        if not acknowledged:
            choice_scan = len(tui.output)
            continue
        handled_request_ids.add(request_id)
        decisions.append(action)
        decision_wave = highest_wave
        progress_deadline = time.monotonic() + stall_timeout
        print(
            f"  …coverage decision through attached TUI: {action} at wave {decision_wave}",
            flush=True,
        )
    raise TimeoutError(
        "campaign did not export a report after coverage-replan handling; "
        f"tail={tui.visible()[-3000:]!r}"
    )


def _completed_coverage(status: dict[str, object]) -> tuple[set[str], set[str]]:
    """Return durable admitted and terminally evaluated catalogue coverage."""
    saturation = status.get("saturation")
    if not isinstance(saturation, dict):
        return set(), set()
    owners = saturation.get("catalogue_cell_owners")
    admitted = (
        {str(row[0]) for row in owners if isinstance(row, list) and row and isinstance(row[0], str)}
        if isinstance(owners, list)
        else set()
    )
    completion = status.get("completion_document")
    cells = completion.get("cells") if isinstance(completion, dict) else None
    terminal = (
        {
            str(row[1])
            for row in cells
            if isinstance(row, list)
            and len(row) >= 3
            and isinstance(row[1], str)
            and str(row[1]).startswith("merlin-injection-taxonomy/")
            and str(row[2]) in {"confirmed", "refuted", "unclear"}
        }
        if isinstance(cells, list)
        else set()
    )
    return admitted, terminal


def _check_meaningful_coverage(label: str, status: dict[str, object]) -> tuple[set[str], set[str]]:
    admitted, terminal = _completed_coverage(status)
    saturation = status.get("saturation")
    saturation = saturation if isinstance(saturation, dict) else {}
    check(
        f"coverage {label}: Recommended frontier admitted without mock-side reduction",
        len(admitted) >= MINIMUM_MEANINGFUL_RECOMMENDED_TECHNIQUES,
        f"minimum={MINIMUM_MEANINGFUL_RECOMMENDED_TECHNIQUES} admitted={len(admitted)}",
    )
    check(
        f"coverage {label}: every admitted technique actually ran",
        terminal == admitted
        and saturation.get("attempted_techniques") == len(admitted)
        and saturation.get("execution_complete") is True,
        f"terminal={sorted(terminal)} attempted={saturation.get('attempted_techniques')}",
    )
    return admitted, terminal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--payload", default=str(ROOT / "target/releases/merlin-0.1.0-darwin-arm64")
    )
    parser.add_argument("--workdir", default=str(ROOT / "target/pty-campaign-uat"))
    parser.add_argument(
        "--keep",
        action="store_true",
        help="deprecated no-op: run artifacts are always kept in a unique subdirectory",
    )
    options = parser.parse_args()

    payload = Path(options.payload).resolve()
    binary = payload / "merlin"
    if not binary.is_file():
        print(f"packaged launcher missing: {binary}", file=sys.stderr)
        return 2
    workdir = Path(options.workdir).resolve()
    # Never delete a user-supplied path: each run gets its own unique
    # subdirectory, and artifacts are always left behind for inspection.
    run_dir = workdir / time.strftime("run-%Y%m%d-%H%M%S")
    suffix = 0
    while run_dir.exists():
        suffix += 1
        run_dir = workdir / f"{time.strftime('run-%Y%m%d-%H%M%S')}-{suffix}"
    home = run_dir / "home"
    workspace = run_dir / "workspace"
    reports_dir = workspace / "target" / "reports"
    home.mkdir(parents=True)
    workspace.mkdir(parents=True)

    target = FixtureTarget()
    llm, brain = make_server(target.origin)
    llm_thread = threading.Thread(target=llm.serve_forever, daemon=True)
    llm_thread.start()
    llm_base_url = f"http://127.0.0.1:{llm.server_address[1]}/v1"
    _seed_home(home, llm_base_url)
    os.environ["MOCK_LLM_DUMP"] = str(run_dir / "mock-requests.jsonl")
    print(f"fixture target: {target.origin}\nmock llm: {llm_base_url}\nHOME: {home}", flush=True)

    tui: Tui | None = None
    instances: list[Tui] = []
    failed = False
    try:
        # -- first launch: provider pre-seeded, daemon boots embedded pg0 ------
        tui = Tui(binary, home, workspace)
        instances.append(tui)
        _settle(tui, 420)
        check("boot: packaged TUI reaches ready state on first launch", True)

        tui.send(f"/targets add pty-target Fixture {target.origin} fixture-model")
        tui.wait('"id": "pty-target"', 60)
        tui.send("/targets select pty-target")
        tui.wait('"id": "pty-target"', 60)
        check("target: fixture target registered and selected through the TUI", True)

        # -- Campaign A: API mode, autonomous composition, pause/resume -------
        _submit_campaign(tui, target.origin, "PTYUAT-API", "autonomous")
        review = _wait_review(tui, 420)
        check("review A: campaign review presented after reconnaissance", True)
        # The RichLog viewport is shorter than the review panel, so upper rows
        # (including the "Execution mode" row) never reach the byte stream;
        # the last table row "Available modes: api, hybrid" is the visible
        # proof that API execution is on offer.
        mode_lines = " | ".join(
            line.strip()[:100]
            for line in review.splitlines()
            if "Available modes" in line or "xecution" in line
        )[:300]
        check(
            "review A: API execution mode shown",
            "Available modes" in review and "api" in review,
            mode_lines,
        )
        check(
            "review A: autonomous specialist composition sealed into the review",
            getattr(brain, "last_specialist_composition", "") == "autonomous",
            (
                "campaign_configure specialist_composition="
                f"{getattr(brain, 'last_specialist_composition', '') or '<missing>'}"
            ),
        )
        tui.wait_idle(300)
        prepared_rows = [
            row
            for row in _json_lines(_cli(binary, home, workspace, "campaign", "list"))
            if isinstance(row, dict) and row.get("status") == "prepared"
        ]
        prepared_ids = {str(row.get("id") or "") for row in prepared_rows}
        if not prepared_ids:
            raise AssertionError("campaign A has no durable prepared campaign id")
        campaign_id = sorted(prepared_ids)[-1]
        tui.send("")  # Enter = Approve [default]
        execute_deadline = time.monotonic() + 180
        approved_status = ""
        while approved_status not in {"executing", "completed", "paused"}:
            if time.monotonic() > execute_deadline:
                raise TimeoutError("approved campaign A did not enter execution")
            tui._pump(0.2)  # noqa: SLF001
            tui._scan_permissions()  # noqa: SLF001
            rows = [
                row
                for row in _json_lines(_cli(binary, home, workspace, "campaign", "list"))
                if isinstance(row, dict) and str(row.get("id") or "") in prepared_ids
            ]
            approved_status = str(rows[-1].get("status") or "") if rows else ""
            if approved_status not in {"executing", "completed", "paused"}:
                time.sleep(0.5)
        target.mark_attack_phase("A")
        check("execute A: approval executes the reviewed manifest", True)

        # Close through the advertised Ctrl+Z path. Campaign listing is scoped
        # to the current client session, so reconnect with the durable ID that
        # was captured before the terminal closed.
        tui.stop()
        tui = None
        status_out = _cli(binary, home, workspace, "campaign", "status", campaign_id)
        status_json = _json_of_text(status_out) or {}
        mid_status = str(status_json.get("status", ""))
        check(
            "restart: durable campaign remains addressable after terminal close",
            mid_status in {"executing", "paused"},
            f"status={mid_status}",
        )

        tui = Tui(binary, home, workspace)
        instances.append(tui)
        _settle(tui, 60, resume_existing=True)
        if mid_status == "paused":
            tui.send(f"/campaign resume {campaign_id}")
            resume_render_deadline = time.monotonic() + 3
            while time.monotonic() < resume_render_deadline:
                tui._pump(0.2)  # noqa: SLF001
        # The TUI only announces reports newer than its launch baseline, so
        # campaign A's report is proven on disk and via the CLI here; the live
        # auto-presentation path is asserted on campaign B, which completes
        # while this TUI instance is attached.
        _, coverage_decisions_a = _wait_report_through_coverage_decisions(
            tui,
            420,
            campaign_id=campaign_id,
        )
        final_json = _json_of_text(
            _cli(binary, home, workspace, "campaign", "status", campaign_id)
        ) or {}
        final_status = str(final_json.get("status", ""))
        check(
            "coverage A: required hypothesis work was continued instead of silently omitted",
            bool(coverage_decisions_a)
            and coverage_decisions_a[0] == "continue"
            and coverage_decisions_a[-1] in {"continue", "stop"},
            f"coverage decisions={coverage_decisions_a}",
        )
        check(
            "restart: explicitly resumed same-session campaign completed",
            final_status == "completed",
            f"status={final_status}",
        )
        coverage_a, _ = _check_meaningful_coverage("A", final_json)
        report_deadline = time.monotonic() + 30
        exported = sorted(reports_dir.glob("*.html"))
        while not exported and time.monotonic() < report_deadline:
            time.sleep(0.2)
            exported = sorted(reports_dir.glob("*.html"))
        check(
            "report A: exported HTML report exists on disk",
            bool(exported),
            str(exported[-1]) if exported else f"nothing under {reports_dir}",
        )

        api_messages = [
            entry for entry in target.invocations("message") if entry.get("attack_phase") == "A"
        ]
        check(
            "mode A: API execution drove the reviewed API adapter against the target",
            len(api_messages) >= 1,
            f"attack-phase messages={len(api_messages)}",
        )

        # -- Campaign B: opinionated Hybrid mode via campaign review ----------
        tui.send("/new")
        tui.wait_idle(120)
        _submit_campaign(tui, target.origin, "PTYUAT-HYBRID", "opinionated")
        review_b = _wait_review(tui, 420)
        check("review B: second campaign review presented", True)
        check(
            "review B: review offers the HYBRID execution-mode choice",
            "Available modes" in review_b and "HYBRID" in review_b.upper(),
        )
        # Choose "Use HYBRID and review again". Transcript waits are unusable
        # here: RichLog repaints the previous review panel on every layout
        # change, so a fresh-output match fires before the mock has processed
        # the new instruction (run 17 toggled hybrid->api this way). Gate on
        # the in-process mock instead: each assessment_prepare it emits
        # increments brain.prepare_count, and last_execution_mode records the
        # prepared mode. The mode choice toggles api<->hybrid, so keep
        # pressing "2" only while the mock's last prepare is not hybrid.
        hybrid_selected = brain.last_execution_mode == "hybrid"
        for _attempt in range(4):
            if hybrid_selected:
                break
            tui.wait_idle(300)
            prepares_before = brain.prepare_count
            tui.send("2")  # "Use HYBRID and review again" (toggles the mode)
            prepare_deadline = time.monotonic() + 420
            while brain.prepare_count == prepares_before:
                if time.monotonic() > prepare_deadline:
                    raise TimeoutError("mock did not emit a revised assessment_prepare")
                tui._pump(0.2)  # noqa: SLF001
                tui._scan_permissions()  # noqa: SLF001
            hybrid_selected = brain.last_execution_mode == "hybrid"
        check(
            "review B: revised review presents Hybrid execution",
            hybrid_selected,
            f"mock-prepared mode={brain.last_execution_mode}",
        )
        # The hybrid manifest is armed only once its review panel renders;
        # snapshotting after the mock's prepare means this marker can only
        # come from the new review, not a stale repaint. Approval is typed
        # explicitly ("approve" is both the armed choice value and the plain
        # prompt the mock's approve branch matches), and execution is gated on
        # the mock's request log — never on transcript echoes, which RichLog
        # repaints can re-emit from campaign A in the same TUI instance.
        _wait_review(tui, 420)
        requests_before_approve = brain.request_count
        execution_output_start = len(tui.output)
        executed_b = False
        for _approve_attempt in range(5):
            tui.wait_idle(180)
            tui.send("approve")
            execute_deadline = time.monotonic() + 60
            while time.monotonic() < execute_deadline:
                tui._pump(0.2)  # noqa: SLF001
                tui._scan_permissions()  # noqa: SLF001
                if any(
                    entry["response"] == "assessment_execute"
                    and entry["n"] > requests_before_approve
                    for entry in brain.request_log
                ):
                    executed_b = True
                    break
            if executed_b:
                break
        if not executed_b:
            raise TimeoutError("mock did not receive assessment_execute for campaign B")
        tui.wait("PTYUAT-EXECUTING", 120)
        check(
            "mode B: the approved campaign executed the hybrid manifest",
            brain.last_execution_mode == "hybrid",
            f"executed mode={brain.last_execution_mode}",
        )
        target.mark_attack_phase("B")
        started_b = re.findall(
            r"Campaign started: (campaign/[0-9a-f-]{36})",
            tui.fresh(execution_output_start),
        )
        campaign_b_running_id = started_b[-1] if started_b else ""
        if not campaign_b_running_id:
            raise AssertionError("opinionated campaign was not durably executing")
        _, coverage_decisions = _wait_report_through_coverage_decisions(
            tui,
            600,
            campaign_id=campaign_b_running_id,
        )
        check(
            "coverage B: missing work was replanned before limitations were exported",
            bool(coverage_decisions)
            and coverage_decisions[0] == "continue"
            and coverage_decisions[-1] in {"continue", "stop"},
            f"coverage decisions={coverage_decisions}",
        )
        check("report B: opinionated hybrid campaign report auto-presented", True)
        listing_b = _cli(binary, home, workspace, "campaign", "list")
        campaigns_b = [
            row
            for row in _json_lines(listing_b)
            if isinstance(row, dict)
            and str(row.get("id", "")).startswith("campaign/")
            and str(row.get("id")) != campaign_id
            and str(row.get("status")) == "completed"
        ]
        campaign_b_id = str(campaigns_b[-1]["id"]) if campaigns_b else ""
        status_b = (
            _json_of_text(_cli(binary, home, workspace, "campaign", "status", campaign_b_id)) or {}
        )
        coverage_b, _ = _check_meaningful_coverage("B", status_b)
        check(
            "coverage: autonomous and opinionated composition preserve the same frontier",
            coverage_a == coverage_b,
            f"autonomous={sorted(coverage_a)} opinionated={sorted(coverage_b)}",
        )
        specialist_events = status_b.get("dynamic_specialist_events", ())
        template_events = [
            event
            for event in specialist_events
            if isinstance(event, dict)
            and isinstance(event.get("creation"), dict)
            and event["creation"].get("kind") == "template"
        ]
        check(
            "composition B: opinionated template specialists persisted in campaign state",
            bool(template_events),
            f"campaign={campaign_b_id} template events={len(template_events)}",
        )
        hybrid_messages = [
            entry for entry in target.invocations("message") if entry.get("attack_phase") == "B"
        ]
        hybrid_uas = {entry["user_agent"] for entry in hybrid_messages}
        check(
            "mode B: hybrid execution transported API steps through the browser context",
            bool(hybrid_messages) and any("Mozilla" in ua for ua in hybrid_uas),
            f"messages={len(hybrid_messages)} user agents={sorted(hybrid_uas)}",
        )

        # -- Campaign C: Browser mode, attempted honestly ----------------------
        executes_before_browser = sum(
            entry["response"] == "assessment_execute" for entry in brain.request_log
        )
        tui.send("/new")
        tui.wait_idle(120)
        _submit_campaign(tui, target.origin, "PTYUAT-BROWSER", "autonomous")
        tui.wait("PTYUAT-BROWSER-FINDING", 420)
        check(
            "mode C: browser-only execution attempted and rejected by the reviewed flow",
            True,
            "finding surfaced in the TUI transcript",
        )
        # The finding's full sentence is cropped by the RichLog viewport (only
        # the marker is guaranteed to reach the byte stream), so the rejection
        # is verified against the mock's authoritative request log: the
        # finding content was served, and this browser-only attempt did not add
        # an execution after the preceding campaigns' legitimate approvals.
        executes = [
            entry["n"] for entry in brain.request_log if entry["response"] == "assessment_execute"
        ]
        finding_served = any(
            "PTYUAT-BROWSER-FINDING" in str(entry["response"]) for entry in brain.request_log
        )
        check(
            "mode C: no browser-adapter campaign was prepared (nothing faked)",
            finding_served and len(executes) == executes_before_browser,
            (
                f"executes_before_browser={executes_before_browser} "
                f"executes={executes} finding_served={finding_served}"
            ),
        )

        # -- Whole-run permission audit (per TUI instance, never cross-counted) --
        per_instance = {
            f"instance-{index}": list(instance.permission_effects)
            for index, instance in enumerate(instances, start=1)
        }
        observational = [
            f"{name}:{entry}"
            for name, effects in per_instance.items()
            for entry in effects
            if entry.split(":", 1)[0] in {"workspace_read", "provider_network", "target_read"}
        ]
        check(
            "permissions: no prompt for observational effects in permission_mode=auto",
            not observational,
            f"prompted effects={per_instance}",
        )
        check(
            "permissions: mutation effects (harness_state/target_network) did prompt",
            any(
                entry.startswith("harness_state")
                for instance in instances
                for entry in instance.permission_effects
            ),
            f"prompted effects={per_instance}",
        )

        role_counts = json.dumps(brain.role_counts, sort_keys=True)
        print("\nmock LLM role counts: " + role_counts, flush=True)
    except (AssertionError, TimeoutError) as error:
        failed = True
        check(f"run aborted: {type(error).__name__}", False, str(error)[:2_000])
    finally:
        for index, instance in enumerate(instances):
            try:
                (run_dir / f"tui-{index}.log").write_bytes(bytes(instance.output))
                (run_dir / f"tui-{index}.keys.log").write_text(
                    "\n".join(f"{at:.3f} {chunk!r}" for at, chunk in instance.keylog)
                )
            except Exception:
                pass
        if tui is not None:
            tui.stop()
        try:
            _cli(binary, home, workspace, "stop")
        except Exception:
            pass
        llm.shutdown()
        target.close()

    print("\n==== PTY campaign acceptance summary ====")
    width = max(len(name) for name, _, _ in RESULTS)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail[:160]}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} assertions passed")
    print(f"run artifacts kept at {run_dir}")
    if failed or passed != len(RESULTS):
        return 1
    return 0


def _json_lines(text: str) -> list[object]:
    parsed = _json_of_text(text)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("campaigns", "items", "results"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return [parsed]
    return []


def _json_of_text(text: str) -> object:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main())
