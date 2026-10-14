from __future__ import annotations

import asyncio
import difflib
import json
import re
import shlex
import textwrap
import time
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rich.console import Group
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual._cells import cell_width_to_column_index
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Static,
    TextArea,
)

from .backend import (
    AgentView,
    BrowserContextView,
    CampaignView,
    ChatBackend,
    DiagnosticCheckView,
    FindingView,
    McpServerView,
    MemoryRecordView,
    OperationalEntryView,
    PackCaseView,
    PackView,
    PermissionAuditView,
    PermissionRuleView,
    PromotionCandidateView,
    PromotionHistoryView,
    ReportView,
    ResumeSessionPage,
    ResumeSessionView,
    ScheduleView,
    SkillView,
    TargetView,
)
from .client_state import ClientState, TranscriptEntry

COMMANDS = (
    "/help",
    "/web",
    "/setup",
    "/setup endpoint",
    "/setup jev",
    "/model",
    "/new",
    "/sessions",
    "/resume",
    "/resume SESSION_ID",
    "/branch MESSAGE_SEQUENCE",
    "/rewind MESSAGE_SEQUENCE",
    "/restore",
    "/context",
    "/doctor",
    "/doctor config",
    "/logs",
    "/logs LIMIT",
    "/history",
    "/history LIMIT",
    "/status",
    "/plugins",
    "/library",
    "/targets",
    "/targets list",
    "/targets add ARGS…",
    "/targets select ID",
    "/targets remove ID",
    "/skills",
    "/skills list",
    "/skills inspect NAME",
    "/skills enable NAME",
    "/skills disable NAME",
    "/mcp",
    "/mcp remote",
    "/mcp list",
    "/mcp add NAME COMMAND ARGS…",
    "/mcp remove NAME",
    "/mcp check NAME",
    "/mcp restart NAME",
    "/mcp disable NAME",
    "/mcp admit NAME DIGEST",
    "/agents",
    "/agents kill all",
    "/tasks",
    "/campaigns",
    "/campaign status ID",
    "/campaign saturation ID stop|continue|limit N",
    "/campaign message ID GUIDANCE",
    "/campaign cancel [ID|all]",
    "/campaign pause ID",
    "/campaign resume ID",
    "/cancel all",
    "/cancel campaign ID",
    "/cancel task ID",
    "/pause campaign ID",
    "/resume campaign ID",
    "/lessons",
    "/agent message ID TEXT…",
    "/agent stop ID",
    "/findings",
    "/findings list",
    "/findings accept ID",
    "/findings reject ID",
    "/report",
    "/report compare OLDER_REVISION NEWER_REVISION",
    "/report [--format html|markdown|csv|json] [--audience internal|customer] [--output PATH]",
    "/support OUTPUT.zip",
    "/mode",
    "/mode prompt",
    "/mode auto",
    "/mode yolo",
    "/permissions",
    "/promotion",
    "/promotion propose ARGS…",
    "/promotion list",
    "/promotion show ID",
    "/promotion evaluate ID",
    "/promotion accept ID REASON…",
    "/promotion certify ID EVALUATION EXPIRES_AT",
    "/promotion reject ID REASON…",
    "/promotion activate ID",
    "/promotion history KIND",
    "/promotion rollback KIND",
    "/browser",
    "/browser contexts",
    "/browser tabs CONTEXT",
    "/browser takeover CONTEXT",
    "/browser cleanup CONTEXT",
    "/browser resume CONTEXT",
    "/memory list",
    "/memory import PATH",
    "/memory export PATH",
    "/memory accept BUNDLE RECORD…",
    "/memory reject BUNDLE RECORD REASON…",
    "/memory delete BUNDLE RECORD REASON…",
    "/clear",
    "/interrupt",
    "/quit",
)

# Keep the interactive viewport bounded even during multi-day sessions. This
# matches the upper retained-row policy used by mature terminal harnesses while
# remaining finite; the durable session transcript remains authoritative.
MAX_RENDERED_TRANSCRIPT_LINES = 10_000
MAX_RENDERED_TRANSCRIPT_BYTES = 4 * 1024 * 1024
_SHUTDOWN_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_CANCEL_SECONDS = 0.25

# Some OpenAI-compatible providers emit several words in one SSE delta. Yield between
# those words so Textual can paint progressive output, but keep the interval short enough
# that presentation never becomes a fake, slow typewriter animation.
_WORD_FRAME_SECONDS = 0.004
_DETAIL_EVENT_PREVIEW = 240


def _stream_pieces(text: str) -> tuple[str, ...]:
    """Split a provider delta at word boundaries without changing its exact text."""
    return tuple(re.findall(r"\S+\s*|\s+", text))


def _thinking_preview(text: str, width: int) -> str:
    """Stream reasoning live into the transcript, dimmed, like the harness CLIs."""
    del width
    return f"· Thinking · {text}"


def _activity_detail_view(text: str) -> str:
    """Keep operational events readable without dumping raw tool result payloads."""
    if not text.startswith("↳ Tool · "):
        return text
    event = text.removeprefix("↳ Tool · ")
    if " completed:" not in event:
        return text
    label, payload = event.split(" completed:", maxsplit=1)
    payload = " ".join(payload.split())
    if not payload:
        return f"↳ Tool · {label} completed"
    if len(payload) > _DETAIL_EVENT_PREVIEW:
        payload = payload[:_DETAIL_EVENT_PREVIEW].rstrip() + "… [payload omitted]"
    return f"↳ Tool · {label} completed\n  Result · {payload}"


COMMAND_HINTS = {
    "/help": "show commands and keyboard shortcuts",
    "/web": "open this session in the local browser (keep this terminal running)",
    "/setup": "configure the provider securely",
    "/setup endpoint": "add a provider endpoint and key for another model",
    "/setup jev": "set or update the JEV judgment API key",
    "/model": "change the active model",
    "/new": "start a new durable session",
    "/sessions": "list durable sessions",
    "/resume": "restore a saved conversation and reconnect its campaigns",
    "/resume SESSION_ID": "restore a saved conversation by session ID",
    "/branch MESSAGE_SEQUENCE": "fork this conversation at a durable message sequence",
    "/rewind MESSAGE_SEQUENCE": "move context to an earlier durable message sequence",
    "/restore": "restore context to the durable transcript head",
    "/context": "inspect transcript sequence, branch lineage, and compaction provenance",
    "/doctor": "run production diagnostics",
    "/doctor config": "show non-secret configuration",
    "/logs": "show sanitized operational logs",
    "/logs LIMIT": "show a bounded number of sanitized operational logs",
    "/history": "show sanitized session history",
    "/history LIMIT": "show a bounded number of session-history rows",
    "/status": "show current runtime state",
    "/plugins": "show provider and tool plugins",
    "/library": "search admitted attack techniques and delivery strategies",
    "/targets": "inspect or select targets",
    "/targets list": "list configured targets",
    "/targets add ARGS…": "add a target",
    "/targets select ID": "select the active target",
    "/targets remove ID": "remove a target",
    "/skills": "inspect or enable skills",
    "/skills list": "list discovered skills",
    "/skills inspect NAME": "inspect one skill",
    "/skills enable NAME": "enable a skill for this session",
    "/skills disable NAME": "disable a skill for this session",
    "/mcp": "inspect MCP servers",
    "/mcp remote": "add an authenticated remote MCP server",
    "/mcp list": "list configured MCP servers",
    "/mcp add NAME COMMAND ARGS…": "add a local MCP server command",
    "/mcp remove NAME": "remove an MCP server",
    "/mcp check NAME": "check and discover an MCP server",
    "/mcp restart NAME": "restart an MCP server",
    "/mcp disable NAME": "disable an MCP server",
    "/mcp admit NAME DIGEST": "admit the exact discovered inventory",
    "/agents": "open task workspace; press A for grouped agents",
    "/agents kill all": "stop active campaigns and agents in the current session",
    "/tasks": "inspect the durable task graph",
    "/campaigns": "inspect and stop campaigns in the current session",
    "/campaign status ID": "show campaign waves, progress, and worker results",
    "/campaign saturation ID stop|continue|limit N": (
        "answer a pending exhaustive-saturation decision"
    ),
    "/campaign message ID GUIDANCE": "guide the Campaign Lead at its next decision boundary",
    "/campaign cancel [ID|all]": "cancel campaign work; retain evidence and keep this session open",
    "/campaign pause ID": "durably pause a running campaign",
    "/campaign resume ID": "continue one paused campaign in this session",
    "/cancel all": "stop every active campaign and task in the current session",
    "/cancel campaign ID": "durably stop one campaign and its tasks",
    "/cancel task ID": "cancel one task; other campaign tasks can continue",
    "/pause campaign ID": "durably pause a running campaign",
    "/resume campaign ID": "continue one paused campaign; does not switch sessions",
    "/lessons": "list reviewed lesson memory",
    "/agent message ID TEXT…": "send a message to a worker",
    "/agent stop ID": "interrupt one live worker attempt; campaign recovery may replace it",
    "/findings": "review finding drafts",
    "/findings list": "list finding drafts and dispositions",
    "/findings accept ID": "accept a finding draft",
    "/findings reject ID": "reject a finding draft",
    "/report": "browse saved campaign reports and export formats",
    "/report compare OLDER_REVISION NEWER_REVISION": "compare two immutable report revisions",
    "/report [--format html|markdown|csv|json] [--audience internal|customer] [--output PATH]": (
        "write the campaign report (HTML by default)"
    ),
    "/support OUTPUT.zip": "write a sanitized local diagnostics bundle",
    "/mode": "change permission mode",
    "/mode prompt": "prompt for effects requiring permission",
    "/mode auto": "automatically allow ordinary safe effects",
    "/mode yolo": "allow bounded effects without prompts",
    "/permissions": "inspect permission rules and decisions",
    "/promotion": "review evaluated improvements",
    "/promotion propose ARGS…": "propose a bounded candidate",
    "/promotion list": "list candidates",
    "/promotion show ID": "inspect a candidate",
    "/promotion evaluate ID": "run a frozen evaluation suite",
    "/promotion accept ID REASON…": "accept an evaluated candidate",
    "/promotion certify ID EVALUATION EXPIRES_AT": "certify an accepted passing route",
    "/promotion reject ID REASON…": "reject a candidate",
    "/promotion activate ID": "activate an accepted candidate",
    "/promotion history KIND": "show promotion history for a kind",
    "/promotion rollback KIND": "restore the previous active version",
    "/browser": "inspect browser contexts",
    "/browser contexts": "list browser contexts",
    "/browser tabs CONTEXT": "list tabs in a browser context",
    "/browser takeover CONTEXT": "take operator control of a context",
    "/browser cleanup CONTEXT": "close and clean a context",
    "/browser resume CONTEXT": "resume a durable browser context",
    "/memory list": "list portable memory records",
    "/memory import PATH": "import a bundle into quarantine",
    "/memory export PATH": "export reviewed memory",
    "/memory accept BUNDLE RECORD…": "accept quarantined records",
    "/memory reject BUNDLE RECORD REASON…": "reject quarantined records",
    "/memory delete BUNDLE RECORD REASON…": "delete a memory record",
    "/clear": "clear the transcript",
    "/interrupt": (
        "interrupt the current turn and its attached swarm; does not cancel a campaign by ID"
    ),
    "/quit": "close Merlin cleanly",
}

_ROOT_COMMANDS = tuple(sorted({command.split()[0] for command in COMMANDS}))
_SUBCOMMANDS = {
    root: tuple(
        sorted(
            {
                parts[1]
                for command in COMMANDS
                if (parts := command.split()) and parts[0] == root and len(parts) > 1
            }
        )
    )
    for root in _ROOT_COMMANDS
}

WORDMARK = (
    "                       .__   .__         ",
    "  _____    ____ _______|  |  |__|  ____  ",
    " /     \\ _/ __ \\\\_  __ \\|  |  |  | /    \\ ",
    "|  Y Y  \\\\  ___/ |  | \\/|  |__|  ||   |  \\",
    "|__|_|  / \\___  >|__|   |____/|__||___|  /",
    "      \\/      \\/                       \\/ ",
)

WELCOME_GUIDANCE = (
    "\nGive Merlin a target and the security objective you want assessed.",
    "Example: Assess https://example.com for prompt injection and data exposure.",
    "Include authorization, scope, credentials, or constraints when they matter.",
    "Type /help for commands · /setup to change provider",
)


def _suggest_command(text: str) -> str | None:
    parts = text.split()
    if not parts:
        return None
    root = parts[0].lower()
    if root not in _ROOT_COMMANDS:
        matches = difflib.get_close_matches(root, _ROOT_COMMANDS, n=1, cutoff=0.6)
        return matches[0] if matches else None
    if len(parts) < 2 or not _SUBCOMMANDS[root]:
        return None
    subcommand = parts[1].lower()
    if subcommand in _SUBCOMMANDS[root]:
        return None
    matches = difflib.get_close_matches(subcommand, _SUBCOMMANDS[root], n=1, cutoff=0.72)
    if not matches:
        return None
    return " ".join((root, matches[0], *parts[2:]))


class Composer(TextArea):
    """Chat composer with Codex-style submit and newline bindings."""

    BINDINGS = [
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("shift+enter,ctrl+j", "newline", "New line", show=False, priority=True),
        Binding("up", "history_up", "Previous prompt", show=False, priority=True),
        Binding("down", "history_down", "Next prompt", show=False, priority=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._submitted_history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""

    class Submitted(Message):
        pass

    def action_submit(self) -> None:
        self.post_message(self.Submitted())

    def action_newline(self) -> None:
        self.insert("\n")

    def remember_submission(self, text: str) -> None:
        """Store an immutable snapshot of a non-empty submitted prompt."""
        if not text:
            return
        snapshot = str(text)
        self._submitted_history = [
            previous for previous in self._submitted_history if previous != snapshot
        ]
        self._submitted_history.append(snapshot)
        del self._submitted_history[:-100]
        self._history_index = None
        self._history_draft = ""

    def _recall_submission(self, text: str) -> None:
        self.text = text
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))
        self.resize_for_content()

    def action_history_up(self) -> None:
        """Move up within text, then recall older prompts at the first line."""
        if not self.selection.is_empty or not self.cursor_at_first_line:
            super().action_cursor_up()
            return
        if not self._submitted_history:
            super().action_cursor_up()
            return
        if self._history_index is None:
            self._history_draft = self.text
            self._history_index = len(self._submitted_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._recall_submission(self._submitted_history[self._history_index])

    def action_history_down(self) -> None:
        """Move down within text, then recall newer prompts at the last line."""
        if not self.selection.is_empty or not self.cursor_at_last_line:
            super().action_cursor_down()
            return
        if self._history_index is None:
            super().action_cursor_down()
            return
        self._history_index += 1
        if self._history_index >= len(self._submitted_history):
            self._history_index = None
            self._recall_submission(self._history_draft)
            return
        self._recall_submission(self._submitted_history[self._history_index])

    def action_delete_to_start_of_line(self) -> None:
        """Make Ctrl+U continue through preceding newlines at column zero."""
        row, column = self.selection.end
        if self.selection.is_empty and column == 0 and row > 0:
            previous_end = (row - 1, len(self.document.get_line(row - 1)))
            self._delete_via_keyboard(previous_end, (row, 0))
            return
        super().action_delete_to_start_of_line()

    def resize_for_content(self) -> None:
        """Grow from one to three visible lines while retaining an internal scrollback."""
        visible_lines = max(1, min(3, self.text.count("\n") + 1))
        # Widget height includes padding: one row above and below leaves one
        # to three visible editable rows and then scrolls internally.
        self.styles.height = visible_lines + 2


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "next_choice", "Next action", show=False),
        Binding("up", "previous_choice", "Previous action", show=False),
        Binding("tab", "next_choice", "Next action", show=False),
        Binding("shift+tab", "previous_choice", "Previous action", show=False),
    ]
    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; background: $background 70%; }
    #dialog {
        width: 64; max-width: 90%; height: auto; max-height: 90%;
        overflow-y: auto; border: round $accent;
        padding: 1 2; background: $surface;
    }
    #dialog Label { margin-bottom: 1; text-style: bold; }
    #dialog Button, #dialog Select { width: 100%; margin-top: 1; }
    """

    def __init__(
        self, title: str, choices: Iterable[str], *, default_index: int | None = None
    ) -> None:
        super().__init__()
        self.title = title
        self.choices = tuple(choices)
        self.default_index = default_index

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title)
            for index, choice in enumerate(self.choices):
                identifier_source = choice.removeprefix("/")
                if choice.startswith("/"):
                    identifier_source = " ".join(identifier_source.split()[:2])
                identifier = re.sub(r"[^A-Za-z0-9_-]+", "-", identifier_source)
                suffix = " [default]" if index == self.default_index else ""
                label = (
                    f"{index + 1}. {choice}{suffix}" if self.default_index is not None else choice
                )
                yield Button(Text(label), id=f"choice-{identifier.strip('-')}")
            yield Button("Close", variant="default", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
            return
        index = list(self.query("#dialog Button"))[:-1].index(event.button)
        self.dismiss(self.choices[index])

    def on_mount(self) -> None:
        list(self.query("#dialog Button"))[
            self.default_index if self.default_index is not None else 0
        ].focus()

    def on_key(self, event: events.Key) -> None:
        if event.key.isdigit() and 1 <= int(event.key) <= len(self.choices):
            event.prevent_default()
            event.stop()
            self.dismiss(self.choices[int(event.key) - 1])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, offset: int) -> None:
        buttons = list(self.query(Button))
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()


class SetupChoiceScreen(ChoiceScreen):
    """Scrollable setup choices with native button focus and activation."""

    BINDINGS = [
        *ChoiceScreen.BINDINGS,
        Binding("down", "next_choice", "Next", show=False),
        Binding("up", "previous_choice", "Previous", show=False),
    ]
    DEFAULT_CSS = """
    SetupChoiceScreen #dialog { max-height: 90%; overflow-y: auto; }
    SetupChoiceScreen #dialog Button { height: auto; min-height: 3; text-align: left; }
    """

    def on_mount(self) -> None:
        self.query(Button).first().focus()


@dataclass(frozen=True, slots=True)
class ProviderSetup:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    vision: bool = False
    context_window_tokens: int | None = None
    effort: str = "default"


@dataclass(frozen=True, slots=True)
class EndpointSetup:
    model: str
    base_url: str
    api_key: str = field(repr=False)


class EndpointScreen(ModalScreen[EndpointSetup | None]):
    DEFAULT_CSS = ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "EndpointScreen")
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Provider endpoint for another model")
            yield Input(
                placeholder="Model ID served by the endpoint",
                name="Endpoint model ID",
                id="endpoint-model",
            )
            yield Input(
                placeholder="Base URL",
                name="Endpoint base URL",
                id="endpoint-base-url",
            )
            yield Input(
                placeholder="API key (stored securely)",
                password=True,
                name="Endpoint API key",
                id="endpoint-api-key",
            )
            yield Button("Save", variant="primary", id="save")
            yield Button("Cancel", id="close")

    def on_mount(self) -> None:
        self.query_one("#endpoint-model", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            model = self.query_one("#endpoint-model", Input).value.strip()
            base_url = self.query_one("#endpoint-base-url", Input).value.strip()
            if not model or not base_url:
                self.notify("Model ID and base URL are required", severity="error")
                return
            self.dismiss(
                EndpointSetup(model, base_url, self.query_one("#endpoint-api-key", Input).value)
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SetupScreen(ModalScreen[ProviderSetup | None]):
    DEFAULT_CSS = ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "SetupScreen")
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(
        self, route: Any, base_url: str = "", model: str = "", effort: str = "default"
    ) -> None:
        super().__init__()
        self.route = route
        self.base_url = base_url
        self.model = model
        self.effort = effort
        self._discovered: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Provider setup")
            yield Input(
                value=self.base_url,
                placeholder="Base URL",
                name="Provider base URL",
                id="setup-base-url",
            )
            yield Input(
                value=self.model,
                placeholder="Model",
                name="Provider model ID",
                id="setup-model",
            )
            yield Input(
                placeholder="API key (stored securely)",
                password=True,
                name="Provider API key",
                id="setup-api-key",
            )
            yield Button("Discover current models", id="discover-models")
            yield Checkbox("Model accepts image input", id="setup-vision")
            yield Input(
                placeholder="Context window tokens (optional; exact documented value)",
                name="Model context window tokens",
                id="setup-context-window",
                type="integer",
            )
            yield Select(
                (
                    ("Provider default", "default"),
                    ("Minimal", "minimal"),
                    ("Low", "low"),
                    ("Medium", "medium"),
                    ("High", "high"),
                    ("Extra high", "xhigh"),
                ),
                value=self.effort,
                allow_blank=False,
                name="Reasoning effort (provider permitting)",
                id="setup-effort",
            )
            yield Button("Save", variant="primary", id="save")
            yield Button("Cancel", id="close")

    def on_mount(self) -> None:
        self.query_one("#setup-base-url", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "discover-models":
            await self._discover_models()
            return
        if event.button.id == "save":
            raw_window = self.query_one("#setup-context-window", Input).value.strip()
            if raw_window and (not raw_window.isdigit() or int(raw_window) < 1_024):
                self.notify("Context window must be an integer of at least 1024", severity="error")
                return
            self.dismiss(
                ProviderSetup(
                    self.query_one("#setup-base-url", Input).value,
                    self.query_one("#setup-model", Input).value,
                    self.query_one("#setup-api-key", Input).value,
                    self.query_one("#setup-vision", Checkbox).value,
                    int(raw_window) if raw_window else None,
                    str(self.query_one("#setup-effort", Select).value),
                )
            )
        else:
            self.dismiss(None)

    async def _discover_models(self) -> None:
        from .provider_catalog import discover_model_catalog

        base_url = self.query_one("#setup-base-url", Input).value.strip()
        api_key = self.query_one("#setup-api-key", Input).value
        discovery = await asyncio.to_thread(
            discover_model_catalog, self.route, api_key, base_url=base_url
        )
        if not discovery.models:
            self.notify(discovery.error or "No current models were reported", severity="error")
            return
        self._discovered = {item.id: item for item in discovery.models}
        self.app.push_screen(
            ChoiceScreen("Choose a currently reported model", tuple(self._discovered)),
            self._select_discovered_model,
        )

    def _select_discovered_model(self, choice: str | None) -> None:
        if choice is None:
            return
        candidate = self._discovered[choice]
        self.query_one("#setup-model", Input).value = candidate.id
        if candidate.vision is not None:
            self.query_one("#setup-vision", Checkbox).value = candidate.vision
        if candidate.context_window_tokens is not None:
            self.query_one("#setup-context-window", Input).value = str(
                candidate.context_window_tokens
            )

    def action_cancel(self) -> None:
        self.dismiss(None)


_JEV_ENABLE_LABEL = "Yes — set up Jev"
_JEV_SKIP_LABEL = "No — keep off (default)"


@dataclass(frozen=True, slots=True)
class JevSetup:
    api_key: str = field(repr=False)


class JevSetupScreen(ModalScreen[JevSetup | None]):
    DEFAULT_CSS = ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "JevSetupScreen")
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, token_configured: bool = False) -> None:
        super().__init__()
        self.token_configured = token_configured

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Judgment model (Jev)")
            yield Input(
                placeholder=(
                    "(configured — leave blank to keep)"
                    if self.token_configured
                    else "Jev API key (get one at tokenra.io)"
                ),
                password=True,
                name="Jev API key",
                id="jev-api-key",
            )
            yield Button("Save", variant="primary", id="save")
            yield Button("Cancel", id="close")

    def on_mount(self) -> None:
        self.query_one("#jev-api-key", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            api_key = self.query_one("#jev-api-key", Input).value.strip()
            if not api_key and not self.token_configured:
                self.notify("Jev API key is required", severity="error")
                return
            self.dismiss(JevSetup(api_key))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True, slots=True)
class ModelSetup:
    worker_model: str
    worker_transport: str
    heterogeneous: bool
    lead_model: str
    lead_transport: str
    recon_model: str
    recon_transport: str
    validator_model: str
    validator_transport: str
    adversarial_explorer_model: str
    adversarial_explorer_transport: str
    role_efforts: Mapping[str, str]
    judgments_jev_enabled: bool = True


class ModelScreen(ModalScreen[ModelSetup | None]):
    DEFAULT_CSS = ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "ModelScreen")
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]
    _CUSTOM_MODEL = "__custom_model__"

    def __init__(
        self,
        current: str = "",
        *,
        models_by_transport: Mapping[str, Iterable[str]] | None = None,
        current_lead: str = "",
        current_recon: str = "",
        current_validator: str = "",
        current_adversarial_explorer: str = "",
        worker_transport: str = "openai_compatible",
        lead_transport: str = "openai_compatible",
        recon_transport: str = "openai_compatible",
        validator_transport: str = "openai_compatible",
        adversarial_explorer_transport: str = "openai_compatible",
        role_efforts: Mapping[str, str] | None = None,
        judgments_jev_enabled: bool = True,
        judgments_jev_token_configured: bool = False,
    ) -> None:
        super().__init__()
        self.current = "" if current in {"loading", "unavailable"} else current
        self.current_lead = current_lead
        self.current_recon = current_recon
        self.current_validator = current_validator
        self.current_adversarial_explorer = current_adversarial_explorer
        self.worker_transport = worker_transport
        self.lead_transport = lead_transport
        self.recon_transport = recon_transport
        self.validator_transport = validator_transport
        self.adversarial_explorer_transport = adversarial_explorer_transport
        self.role_efforts = {role: str(effort) for role, effort in (role_efforts or {}).items()}
        self.judgments_jev_enabled = judgments_jev_enabled
        self.judgments_jev_token_configured = judgments_jev_token_configured
        self.models_by_transport = {
            transport: tuple(dict.fromkeys(model.strip() for model in models if model.strip()))
            for transport, models in (models_by_transport or {}).items()
        }
        self._remember_model(self.worker_transport, self.current)
        self._remember_model(self.lead_transport, self.current_lead)
        self._remember_model(self.recon_transport, self.current_recon)
        self._remember_model(self.validator_transport, self.current_validator)
        self._remember_model(self.adversarial_explorer_transport, self.current_adversarial_explorer)

    def _remember_model(self, transport: str, model: str) -> None:
        if not model:
            return
        current = self.models_by_transport.get(transport, ())
        self.models_by_transport[transport] = tuple(dict.fromkeys((*current, model)))

    def _model_options(self, transport: str) -> tuple[tuple[str, str], ...]:
        return tuple((model, model) for model in self.models_by_transport.get(transport, ())) + (
            ("Custom model ID…", self._CUSTOM_MODEL),
        )

    @staticmethod
    def _transport_options() -> tuple[tuple[str, str], ...]:
        return (
            ("OpenAI-compatible endpoint", "openai_compatible"),
            ("OpenAI Responses (native)", "openai_responses"),
            ("Anthropic Messages (native)", "anthropic_messages"),
            ("Gemini (native)", "gemini"),
            ("Codex App Server (Merlin tools supported)", "codex_app_server"),
        )

    @staticmethod
    def _effort_options() -> tuple[tuple[str, str], ...]:
        return (
            ("Provider default", "default"),
            ("Minimal", "minimal"),
            ("Low", "low"),
            ("Medium", "medium"),
            ("High", "high"),
            ("Extra high", "xhigh"),
        )

    def _role_effort(self, role: str) -> str:
        return self.role_efforts.get(role, "default")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Model routing")
            yield Label("Workers")
            yield Select(
                self._model_options(self.worker_transport),
                value=self.current or self._CUSTOM_MODEL,
                allow_blank=False,
                name="Workers model",
                id="worker-model",
            )
            yield Input(placeholder="Custom workers model ID", id="worker-model-custom")
            yield Select(
                self._transport_options(),
                value=self.worker_transport,
                name="Provider transport",
                id="worker-transport",
            )
            yield Label("Workers reasoning effort (provider permitting)", classes="effort-label")
            yield Select(
                self._effort_options(),
                value=self._role_effort("workers"),
                allow_blank=False,
                name="Workers reasoning effort",
                id="worker-effort",
            )
            with Vertical(id="lead-routing"):
                yield Label("Campaign Lead")
                yield Select(
                    self._model_options(self.lead_transport),
                    value=self.current_lead or self.current or self._CUSTOM_MODEL,
                    allow_blank=False,
                    name="Campaign Lead model",
                    id="lead-model",
                )
                yield Input(placeholder="Custom Campaign Lead model ID", id="lead-model-custom")
                yield Select(
                    (
                        ("OpenAI-compatible endpoint", "openai_compatible"),
                        ("OpenAI Responses (native)", "openai_responses"),
                        ("Anthropic Messages (native)", "anthropic_messages"),
                        ("Gemini (native)", "gemini"),
                        ("Codex App Server (Merlin tools supported)", "codex_app_server"),
                        ("Claude CLI (no Merlin tools)", "claude_cli"),
                        ("Kimi ACP (no tools; provider-default effort)", "kimi_acp"),
                    ),
                    value=self.lead_transport,
                    name="Campaign Lead transport",
                    id="lead-transport",
                )
                yield Label(
                    "Campaign Lead reasoning effort (provider permitting)",
                    classes="effort-label",
                )
                yield Select(
                    self._effort_options(),
                    value=self._role_effort("lead"),
                    allow_blank=False,
                    name="Campaign Lead reasoning effort",
                    id="lead-effort",
                )
            with Vertical(id="recon-routing"):
                yield Label("Recon")
                yield Select(
                    self._model_options(self.recon_transport),
                    value=self.current_recon or self.current or self._CUSTOM_MODEL,
                    allow_blank=False,
                    name="Reconnaissance model",
                    id="recon-model",
                )
                yield Input(placeholder="Custom Recon model ID", id="recon-model-custom")
                yield Select(
                    self._transport_options(),
                    value=self.recon_transport,
                    name="Recon transport",
                    id="recon-transport",
                )
                yield Label("Recon reasoning effort (provider permitting)", classes="effort-label")
                yield Select(
                    self._effort_options(),
                    value=self._role_effort("recon"),
                    allow_blank=False,
                    name="Recon reasoning effort",
                    id="recon-effort",
                )
            with Vertical(id="validator-routing"):
                yield Label("Validator")
                yield Select(
                    self._model_options(self.validator_transport),
                    value=self.current_validator or self.current or self._CUSTOM_MODEL,
                    allow_blank=False,
                    name="Validator model",
                    id="validator-model",
                )
                yield Input(placeholder="Custom Validator model ID", id="validator-model-custom")
                yield Select(
                    self._transport_options(),
                    value=self.validator_transport,
                    allow_blank=False,
                    name="Validator transport",
                    id="validator-transport",
                )
                yield Label(
                    "Validator reasoning effort (provider permitting)", classes="effort-label"
                )
                yield Select(
                    self._effort_options(),
                    value=self._role_effort("validator"),
                    allow_blank=False,
                    name="Validator reasoning effort",
                    id="validator-effort",
                )
            with Vertical(id="adversarial-explorer-routing"):
                yield Label("Adversarial Explorer")
                yield Select(
                    self._model_options(self.adversarial_explorer_transport),
                    value=self.current_adversarial_explorer or self.current or self._CUSTOM_MODEL,
                    allow_blank=False,
                    name="Adversarial Explorer model",
                    id="adversarial-explorer-model",
                )
                yield Input(
                    placeholder="Custom Adversarial Explorer model ID",
                    id="adversarial-explorer-model-custom",
                )
                yield Select(
                    self._transport_options(),
                    value=self.adversarial_explorer_transport,
                    allow_blank=False,
                    name="Adversarial Explorer transport",
                    id="adversarial-explorer-transport",
                )
                yield Label(
                    "Adversarial Explorer reasoning effort (provider permitting)",
                    classes="effort-label",
                )
                yield Select(
                    self._effort_options(),
                    value=self._role_effort("adversarial-explorer"),
                    allow_blank=False,
                    name="Adversarial Explorer reasoning effort",
                    id="adversarial-explorer-effort",
                )
            with Vertical(id="judgments-routing"):
                yield Checkbox(
                    "Use judgment model (Jev) for grading and triage",
                    value=self.judgments_jev_enabled,
                    id="judgments-jev-enabled",
                )
            yield Button("Save routing", variant="primary", id="save")
            yield Button("Cancel", id="close")

    def on_mount(self) -> None:
        self.query_one("#worker-model", Select).focus()
        self._show_custom_model("worker", self.current == "")
        self._show_custom_model("lead", not (self.current_lead or self.current))
        self._show_custom_model("recon", not (self.current_recon or self.current))
        self._show_custom_model("validator", not (self.current_validator or self.current))
        self._show_custom_model(
            "adversarial-explorer", not (self.current_adversarial_explorer or self.current)
        )

    @on(Select.Changed, "#worker-model")
    def _worker_model_changed(self, event: Select.Changed) -> None:
        self._show_custom_model("worker", event.value == self._CUSTOM_MODEL)

    @on(Select.Changed, "#lead-model")
    def _lead_model_changed(self, event: Select.Changed) -> None:
        self._show_custom_model("lead", event.value == self._CUSTOM_MODEL)

    @on(Select.Changed, "#recon-model")
    def _recon_model_changed(self, event: Select.Changed) -> None:
        self._show_custom_model("recon", event.value == self._CUSTOM_MODEL)

    @on(Select.Changed, "#validator-model")
    def _validator_model_changed(self, event: Select.Changed) -> None:
        self._show_custom_model("validator", event.value == self._CUSTOM_MODEL)

    @on(Select.Changed, "#adversarial-explorer-model")
    def _adversarial_explorer_model_changed(self, event: Select.Changed) -> None:
        self._show_custom_model("adversarial-explorer", event.value == self._CUSTOM_MODEL)

    @on(Select.Changed, "#worker-transport")
    def _worker_transport_changed(self, event: Select.Changed) -> None:
        self._replace_model_options("worker", str(event.value))

    @on(Select.Changed, "#lead-transport")
    def _lead_transport_changed(self, event: Select.Changed) -> None:
        self._replace_model_options("lead", str(event.value))

    @on(Select.Changed, "#recon-transport")
    def _recon_transport_changed(self, event: Select.Changed) -> None:
        self._replace_model_options("recon", str(event.value))

    @on(Select.Changed, "#validator-transport")
    def _validator_transport_changed(self, event: Select.Changed) -> None:
        self._replace_model_options("validator", str(event.value))

    @on(Select.Changed, "#adversarial-explorer-transport")
    def _adversarial_explorer_transport_changed(self, event: Select.Changed) -> None:
        self._replace_model_options("adversarial-explorer", str(event.value))

    def _replace_model_options(self, tier: str, transport: str) -> None:
        selector = self.query_one(f"#{tier}-model", Select)
        current = str(selector.value)
        options = self._model_options(transport)
        selector.set_options(options)
        discovered = self.models_by_transport.get(transport, ())
        selector.value = (
            current
            if current in discovered
            else discovered[0]
            if discovered
            else self._CUSTOM_MODEL
        )

    def _show_custom_model(self, tier: str, visible: bool) -> None:
        field = self.query_one(f"#{tier}-model-custom", Input)
        field.display = visible

    def _selected_model(self, tier: str) -> str:
        selected = self.query_one(f"#{tier}-model", Select).value
        if selected == self._CUSTOM_MODEL:
            return self.query_one(f"#{tier}-model-custom", Input).value
        return str(selected)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save":
            self.dismiss(None)
            return
        worker_transport = self.query_one("#worker-transport", Select).value
        lead_transport = self.query_one("#lead-transport", Select).value
        recon_transport = self.query_one("#recon-transport", Select).value
        validator_transport = self.query_one("#validator-transport", Select).value
        explorer_transport = self.query_one("#adversarial-explorer-transport", Select).value
        role_efforts = {
            "workers": str(self.query_one("#worker-effort", Select).value),
            "lead": str(self.query_one("#lead-effort", Select).value),
            "recon": str(self.query_one("#recon-effort", Select).value),
            "validator": str(self.query_one("#validator-effort", Select).value),
            "adversarial-explorer": str(
                self.query_one("#adversarial-explorer-effort", Select).value
            ),
        }
        if lead_transport == "kimi_acp" and role_efforts["lead"] != "default":
            self.notify(
                "Kimi ACP does not expose a reasoning-effort option; choose Provider default",
                severity="error",
            )
            return
        judgments_jev_enabled = self.query_one("#judgments-jev-enabled", Checkbox).value
        if judgments_jev_enabled and not self.judgments_jev_token_configured:
            self.notify(
                "No Jev API key configured — run /setup and enable the judgment model (Jev)",
                severity="warning",
            )
        self.dismiss(
            ModelSetup(
                self._selected_model("worker"),
                str(worker_transport),
                True,
                self._selected_model("lead"),
                str(lead_transport),
                self._selected_model("recon"),
                str(recon_transport),
                self._selected_model("validator"),
                str(validator_transport),
                self._selected_model("adversarial-explorer"),
                str(explorer_transport),
                role_efforts,
                judgments_jev_enabled,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


async def _configured_model_screen(current: str) -> ModelScreen:
    """Build the routing dialog from live catalogues plus persisted model IDs."""
    try:
        from merlin_config import CredentialStore, load_config

        from .provider_catalog import discover_routing_models

        config = load_config()
        provider = config.values.get("provider", {})
        if not isinstance(provider, Mapping):
            raise ValueError("provider configuration is not a mapping")
        worker = str(provider.get("model", current))
        roles = config.provider_role_models
        lead = roles.get("lead", "")
        recon = roles.get("recon", "")
        validator = roles.get("validator", "")
        adversarial_explorer = roles.get("adversarial-explorer", "")
        credentials = CredentialStore().load()
        discovered = await discover_routing_models(
            provider,
            str(credentials.get("api_key", credentials.get("openai_api_key", ""))),
        )
        models_by_transport = {
            transport: tuple(candidate.id for candidate in result.models)
            for transport, result in discovered.items()
        }
        for model in (worker, *roles.values(), *config.provider_model_transports.keys()):
            transport = config.provider_model_transports.get(model, "openai_compatible")
            models_by_transport[transport] = tuple(
                dict.fromkeys((*models_by_transport.get(transport, ()), model))
            )
        model_efforts = config.provider_model_efforts
        default_effort = str(provider.get("effort", "default"))

        def role_effort(role_model: str) -> str:
            return model_efforts.get(role_model, default_effort)

        return ModelScreen(
            worker,
            models_by_transport=models_by_transport,
            current_lead=lead,
            current_recon=recon,
            current_validator=validator,
            current_adversarial_explorer=adversarial_explorer,
            worker_transport=config.provider_model_transports.get(worker, "openai_compatible"),
            lead_transport=config.provider_model_transports.get(lead, "openai_compatible"),
            recon_transport=config.provider_model_transports.get(recon, "openai_compatible"),
            validator_transport=config.provider_model_transports.get(
                validator, "openai_compatible"
            ),
            adversarial_explorer_transport=config.provider_model_transports.get(
                adversarial_explorer, "openai_compatible"
            ),
            role_efforts={
                "workers": role_effort(worker),
                "lead": role_effort(lead or worker),
                "recon": role_effort(recon or worker),
                "validator": role_effort(validator or worker),
                "adversarial-explorer": role_effort(adversarial_explorer or worker),
            },
            judgments_jev_enabled=config.judgments.jev_decisions_enabled,
            judgments_jev_token_configured=bool(config.judgments.jev_token),
        )
    except (OSError, TypeError, ValueError):
        return ModelScreen(current, models_by_transport={"openai_compatible": (current,)})


async def _check_jev_health(token: str) -> str:
    """Probe the Jev API with one tiny noul call; never raise — saving proceeds."""
    import httpx

    try:
        from merlin_config import load_config

        url = load_config().judgments.jev_url
    except Exception:  # noqa: BLE001 - fall back to the public endpoint
        url = "https://api.typesafe.ai/v1/systemone"
    payload = {
        "state": "test",
        "model": "jev-latest",
        "questions": {
            "health": {
                "type": "noul",
                "instructions": "Is this a health check?",
                "criteria": {"true": "Health check", "false": "Not a health check"},
            }
        },
    }
    try:
        response = await asyncio.to_thread(
            httpx.post,
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=5.0,
        )
        response.raise_for_status()
    except Exception:
        return "Jev health check failed; configuration was saved anyway."
    return "Jev health check passed."


@dataclass(frozen=True, slots=True)
class RemoteMcpSetup:
    name: str
    url: str
    credential_handle: str
    secret: str = field(repr=False)


class RemoteMcpScreen(ModalScreen[RemoteMcpSetup | None]):
    DEFAULT_CSS = ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "RemoteMcpScreen")
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add authenticated remote MCP")
            yield Input(
                placeholder="Server name", name="Remote MCP server name", id="mcp-remote-name"
            )
            yield Input(placeholder="HTTPS MCP URL", name="Remote MCP URL", id="mcp-remote-url")
            yield Input(
                placeholder="Credential handle",
                name="Remote MCP credential handle",
                id="mcp-credential-handle",
            )
            yield Input(
                placeholder="Bearer credential (stored securely)",
                password=True,
                name="Remote MCP bearer credential",
                id="mcp-credential-secret",
            )
            yield Button("Save", variant="primary", id="mcp-remote-save")
            yield Button("Cancel", id="close")

    def on_mount(self) -> None:
        self.query_one("#mcp-remote-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "mcp-remote-save":
            self.dismiss(None)
            return
        self.dismiss(
            RemoteMcpSetup(
                self.query_one("#mcp-remote-name", Input).value,
                self.query_one("#mcp-remote-url", Input).value,
                self.query_one("#mcp-credential-handle", Input).value,
                self.query_one("#mcp-credential-secret", Input).value,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class SelectableDrawerText(TextArea):
    """Read-only drawer content with native mouse and keyboard selection."""

    DEFAULT_CSS = """
    SelectableDrawerText {
        height: 1fr;
        border: none;
        background: transparent;
        padding: 0;
    }
    """

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(
            content,
            read_only=True,
            show_line_numbers=False,
            soft_wrap=True,
            **kwargs,
        )
        self._follow_tail = True

    def update(self, content: object, *, preserve_scroll: bool = False) -> None:
        previous_offset = self.scroll_offset
        self.load_text(str(content))
        if preserve_scroll or not self._follow_tail:
            self.call_after_refresh(
                self.scroll_to,
                x=previous_offset.x,
                y=previous_offset.y,
                animate=False,
            )

    def render(self) -> Text:
        return Text(self.text)

    def reveal_line(self, line: int) -> None:
        """Reveal a logical row through TextArea's soft-wrap-aware cursor mapping."""
        self.call_after_refresh(self._reveal_line, line)

    def _reveal_line(self, line: int) -> None:
        self.move_cursor((line, 0))
        self.scroll_cursor_visible(animate=False)

    def follow_latest(self) -> None:
        if self._follow_tail and not self.is_vertical_scrollbar_grabbed:
            self.call_after_refresh(self._scroll_to_tail)

    def _scroll_to_tail(self) -> None:
        if self._follow_tail and not self.is_vertical_scrollbar_grabbed:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._follow_tail = False
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def scroll_page_up(self, **kwargs: Any) -> None:
        self._follow_tail = False
        super().scroll_page_up(**kwargs)

    def scroll_page_down(self, **kwargs: Any) -> None:
        super().scroll_page_down(**kwargs)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def action_scroll_end(self) -> None:
        self._follow_tail = True
        super().action_scroll_end()

    def _resume_follow_if_at_end(self) -> None:
        if self.is_vertical_scroll_end:
            self._follow_tail = True


class StyledDrawerText(RichLog):
    """Rich-styled, scrollable drawer content for the agent swarm monitor."""

    def __init__(self, content: Text | str = "", **kwargs: Any) -> None:
        super().__init__(wrap=True, markup=False, auto_scroll=False, min_width=1, **kwargs)
        self._plain = ""
        self._content = Text()
        self._render_width = 0
        if content:
            self.update(content)

    def update(self, content: Text | str, *, preserve_scroll: bool = False) -> None:
        text = content if isinstance(content, Text) else Text(str(content))
        previous_offset = self.scroll_offset
        self._plain = text.plain
        self._content = text.copy()
        self._reflow_content()
        self.call_after_refresh(self._reflow_content)
        if preserve_scroll:
            self.call_after_refresh(
                self.scroll_to,
                x=previous_offset.x,
                y=previous_offset.y,
                animate=False,
            )

    def _reflow_content(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 1:
            return
        previous_offset = self.scroll_offset
        self._render_width = width
        self.clear()
        self.write(self._content, width=width)
        self.scroll_to(x=previous_offset.x, y=previous_offset.y, animate=False)

    def on_show(self) -> None:
        self.call_after_refresh(self._reflow_content)

    def on_resize(self, event: events.Resize) -> None:
        if self.scrollable_content_region.width != self._render_width:
            self.call_after_refresh(self._reflow_content)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted and round(old_value) != round(new_value):
            # Repaint neighboring pane borders too: partial scroll damage must
            # not retain fragments from the previous expanded pane geometry.
            self.screen.refresh(repaint=True)

    def reveal_line(self, line: int) -> None:
        self.call_after_refresh(self.scroll_to, y=line, animate=False)

    @property
    def plain(self) -> str:
        return self._plain


class LiveSwarmLog(RichLog):
    """The live swarm strip is also an entry point to individual worker details."""

    class OpenDetails(Message):
        pass

    def write(self, content: Any, *args: Any, **kwargs: Any) -> LiveSwarmLog:
        self._snapshot = content.copy() if isinstance(content, Text) else Text(str(content))
        self._reflow_snapshot()
        self.call_after_refresh(self._reflow_snapshot)
        return self

    def _reflow_snapshot(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 1 or not hasattr(self, "_snapshot"):
            return
        self._snapshot_width = width
        super().clear()
        super().write(self._snapshot, width=width)

    def on_show(self) -> None:
        self.call_after_refresh(self._reflow_snapshot)

    def on_resize(self, event: events.Resize) -> None:
        if self.scrollable_content_region.width != getattr(self, "_snapshot_width", 0):
            self.call_after_refresh(self._reflow_snapshot)

    def on_mount(self) -> None:
        self.animating = False
        self._animation_frame = 0
        self.set_interval(0.2, self._advance_indicator)

    def _advance_indicator(self) -> None:
        if self.animating and self.has_class("visible") and self.app.screen is self.screen:
            self._animation_frame += 1
            self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        line_index = int(self.scroll_y) + y
        if self.animating and line_index < len(self.lines):
            line = self.lines[line_index].text
            label = next(
                (
                    label
                    for label in ("Working", "Reviewing wave results", "Finishing current work")
                    if line[0:1] in ("◐", "◓", "◑", "◒") and line[1:].startswith(f" {label}")
                ),
                None,
            )
            if label is not None:
                spinner = ("◐", "◓", "◑", "◒")[self._animation_frame % 4]
                dots = "." * ((self._animation_frame // 2) % 4)
                prefix = f"{spinner} {label}{dots:<3}"
                color = ("#ffad33", "#ffc568", "#ffe0a3", "#ffc568")[
                    (self._animation_frame // 2) % 4
                ]
                return Strip.join(
                    (
                        Strip([Segment(prefix, Style(color=color, bold=True))]),
                        strip.crop(len(prefix)),
                    )
                )
        return strip

    def on_click(self) -> None:
        self.post_message(self.OpenDetails())


class OverviewScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    OverviewScreen { align: right middle; background: $background 35%; }
    #overview {
        width: 76; max-width: 68%; height: 100%;
        border-left: thick $accent; padding: 1 2; background: $surface;
    }
    #overview-title { margin-bottom: 1; text-style: bold; }
    #overview-content { height: 1fr; }
    #overview-close { width: 100%; margin-top: 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("tab", "next_choice", "Next action", show=False),
        Binding("shift+tab", "previous_choice", "Previous action", show=False),
    ]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.heading = title
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="overview"):
            yield Label(self.heading, id="overview-title")
            yield SelectableDrawerText(self.content, id="overview-content")
            yield Button("Close", id="overview-close")

    def on_mount(self) -> None:
        self.query_one(Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "overview-close":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, offset: int) -> None:
        buttons = list(self.query(Button))
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()


class LibraryScreen(ModalScreen[None]):
    """Keyboard-first search over the digest-admitted attack catalogue."""

    DEFAULT_CSS = """
    LibraryScreen { align: center middle; background: $background 70%; }
    #library-dialog {
        width: 104; max-width: 96%; height: 92%; border: round $accent;
        padding: 1 2; background: $surface;
    }
    #library-title { text-style: bold; }
    #library-help { color: $text-muted; margin-bottom: 1; }
    #library-search, #library-cases, #library-close { width: 100%; margin-top: 1; }
    #library-detail-scroll { height: 1fr; margin-top: 1; }
    #library-detail { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, packs: tuple[PackView, ...]) -> None:
        super().__init__()
        self._rows = tuple((pack, case) for pack in packs for case in pack.cases)
        self._visible = self._rows

    def compose(self) -> ComposeResult:
        with Vertical(id="library-dialog"):
            yield Label(
                f"Attack Library · {len(self._rows)} admitted techniques", id="library-title"
            )
            yield Static(
                "Type to filter · Tab to results · ↑/↓ select · Enter inspect · Esc close",
                id="library-help",
                markup=False,
            )
            yield Input(
                placeholder="Search technique, strategy, objective, tag, or ID", id="library-search"
            )
            yield Select(
                self._options(self._visible),
                value=self._visible[0][1].id if self._visible else Select.BLANK,
                id="library-cases",
                allow_blank=True,
            )
            with VerticalScroll(id="library-detail-scroll"):
                yield Static(
                    self._detail(self._visible[0]) if self._visible else "No matches.",
                    id="library-detail",
                    markup=False,
                )
            yield Button("Close", id="library-close")

    def on_mount(self) -> None:
        self.query_one("#library-search", Input).focus()

    @on(Input.Changed, "#library-search")
    def _filter(self, event: Input.Changed) -> None:
        terms = tuple(event.value.lower().split())
        self._visible = tuple(
            row for row in self._rows if all(term in self._search_text(row) for term in terms)
        )
        selector = self.query_one("#library-cases", Select)
        selector.set_options(self._options(self._visible))
        if self._visible:
            selector.value = self._visible[0][1].id
            self.query_one("#library-detail", Static).update(self._detail(self._visible[0]))
        else:
            self.query_one("#library-detail", Static).update("No matching techniques.")

    @on(Select.Changed, "#library-cases")
    def _select(self, event: Select.Changed) -> None:
        selected = next((row for row in self._visible if row[1].id == event.value), None)
        if selected is not None:
            self.query_one("#library-detail", Static).update(self._detail(selected))

    @on(Button.Pressed, "#library-close")
    def _close(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @staticmethod
    def _options(rows: tuple[tuple[PackView, PackCaseView], ...]) -> tuple[tuple[str, str], ...]:
        return tuple((f"{case.title} · {case.strategy}", case.id) for _, case in rows)

    @staticmethod
    def _search_text(row: tuple[PackView, PackCaseView]) -> str:
        pack, case = row
        return " ".join(
            (
                pack.id,
                pack.title,
                case.id,
                case.title,
                case.instructions,
                case.strategy,
                *case.objectives,
                *case.tags,
                *case.success_hints,
            )
        ).lower()

    @staticmethod
    def _detail(row: tuple[PackView, PackCaseView]) -> str:
        pack, case = row
        objectives = ", ".join(case.objectives) or "Not declared"
        surfaces = (
            ", ".join(
                tag
                for tag in case.tags
                if tag
                in {
                    "direct-user-channel",
                    "conversational",
                    "document-embedded",
                    "inter-agent-message",
                    "media-carried",
                    "persistent-memory",
                    "prior-model-output",
                    "retrieval-context",
                }
            )
            or "Not declared"
        )
        tags = ", ".join(case.tags) or "none"
        hints = "\n- ".join(case.success_hints) or "Not declared"
        return (
            f"{case.title}\n{case.id}\n\nWHAT IT TESTS\n{case.instructions}\n\n"
            f"HOW IT IS DELIVERED\nStrategy: {case.strategy}\nSurface: {surfaces}\n\n"
            f"OBJECTIVES\n{objectives}\n\nORACLE AND CONTROL\n"
            f"Oracle: {case.oracle_id or 'Not declared'}\n"
            f"Control: {case.control_case_id or 'Not required'}\n\n"
            f"EVIDENCE HINTS\n- {hints}\n\nTAGS\n{tags}\n\nPACK\n{pack.title} ({pack.id})"
        )


class LiveDetailScroll(VerticalScroll):
    """Scrollable details pane that follows new output until the operator scrolls away."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._follow_tail = True

    def follow_latest(self) -> None:
        if self._follow_tail and not self.is_vertical_scrollbar_grabbed:
            self.call_after_refresh(self._scroll_to_tail)

    def _scroll_to_tail(self) -> None:
        if self._follow_tail and not self.is_vertical_scrollbar_grabbed:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._follow_tail = False
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def scroll_page_up(self, **kwargs: Any) -> None:
        self._follow_tail = False
        super().scroll_page_up(**kwargs)

    def scroll_page_down(self, **kwargs: Any) -> None:
        super().scroll_page_down(**kwargs)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def action_scroll_end(self) -> None:
        self._follow_tail = True
        super().action_scroll_end()

    def _resume_follow_if_at_end(self) -> None:
        if self.is_vertical_scroll_end:
            self._follow_tail = True


class ActiveWorkScreen(OverviewScreen):
    """Live, toggleable view of the current model reasoning and tool activity."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+o", "cancel", "Close details", show=False),
        Binding("tab", "next_choice", "Next action", show=False),
        Binding("shift+tab", "previous_choice", "Previous action", show=False),
    ]

    def __init__(self, content_provider: Callable[[], str]) -> None:
        super().__init__("Active work details", "")
        self._content_provider = content_provider
        self._rendered_content = ""
        self._refresh_timer: Any | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="overview"):
            yield Label(self.heading, id="overview-title")
            yield SelectableDrawerText("", id="overview-content")
            yield Button("Close", id="overview-close")

    def on_mount(self) -> None:
        super().on_mount()
        self.refresh_details()
        self._refresh_timer = self.set_interval(0.1, self.refresh_details)

    def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def refresh_details(self) -> None:
        content = self._content_provider()
        if content == self._rendered_content:
            return
        self._rendered_content = content
        details = self.query_one("#overview-content", SelectableDrawerText)
        details.update(content)
        details.follow_latest()


def _worker_activity(agent: AgentView) -> str:
    """Summarize recorded activity without claiming unobserved model progress."""
    state = _agent_display_state(agent.latest_event)
    if state == "failed":
        return (
            "Retry scheduled"
            if any("retry scheduled" in row for row in agent.activity)
            else "Failed"
        )
    if state != "working":
        return {
            "queued": "Waiting for admission",
            "completed": "Finished",
            "interrupted": "Interrupted",
            "cancelled": "Cancelled",
        }.get(state, state.title())
    for row in reversed(agent.activity):
        if "heartbeat" in row.lower():
            continue
        lower = row.lower()
        if "thinking" in lower:
            return "Thinking · recorded checkpoint"
        if "sending" in lower:
            return "Processing target exchange · recorded checkpoint"
        if "planning" in lower:
            return "Planning next action · recorded checkpoint"
        return _short_activity(row, 100)
    return "Working · waiting for next update"


def _workspace_worker_label(agent: AgentView) -> str:
    if agent.technique.startswith("capability/"):
        surface = agent.technique.removeprefix("capability/")
        return surface.replace("_", " ").replace("/", " · ").capitalize() + " probe"
    return _inferred_task_group_label((agent,))


class AgentOutputText(SelectableDrawerText):
    """Native selection; live updates wait while a selection is held."""

    BINDINGS = [Binding("ctrl+shift+c", "copy", "Copy selection", priority=True)]

    @property
    def plain(self) -> str:
        return self.text

    def update(self, content: object, *, preserve_scroll: bool = False) -> None:
        text = content.plain if isinstance(content, Text) else str(content)
        if preserve_scroll and (not self.selection.is_empty or self._selecting):
            return
        if text == self.text:
            return
        super().update(text, preserve_scroll=preserve_scroll)


def _workspace_specialist_label(agent: AgentView) -> str:
    if agent.profile == "Unassigned/structural tasks":
        if agent.owner and agent.owner != "unassigned":
            return "Worker · " + agent.owner.rsplit("/", 1)[-1].replace("_", " ")
        return "Worker pending assignment"
    return agent.profile or agent.specialist_instance_id or agent.name or "Unassigned specialist"


def _workspace_task_label(agent: AgentView) -> str:
    if agent.task.startswith(("DIRECT OBSERVATION for", "DIRECT BASELINE for", "BENIGN CONTROL")):
        stage = agent.execution_stage or _agent_stage(agent)
        label = _workspace_worker_label(agent)
        language = f" · language: {agent.requested_language}" if agent.requested_language else ""
        return f"{stage.capitalize()} test: {label}{language}"
    return _agent_assignment_label(agent)


class AgentsOverviewScreen(ModalScreen[None]):
    """Agent identities and their assignments, alongside the existing task workspace."""

    BINDINGS = [
        Binding("escape", "back", "Task workspace", priority=True),
        Binding("t", "back", "Task workspace", priority=True),
    ]
    DEFAULT_CSS = """
    AgentsOverviewScreen { background: $background; padding: 1; }
    #agents-overview-heading { height: 2; color: cyan; text-style: bold; }
    #agents-overview-body { height: 1fr; }
    #agents-overview-picker { width: 30%; min-width: 24; max-width: 42; }
    #agents-overview-detail { width: 1fr; border: solid $accent; padding: 0 1; }
    #agents-overview-help { height: 1; color: $text-muted; }
    """

    def __init__(self, backend: ChatBackend, session_id: str) -> None:
        super().__init__()
        self.backend = backend
        self.session_id = session_id
        self._groups: dict[str, tuple[AgentView, ...]] = {}
        self._selected: str | None = None
        self._timer: Any = None
        self._closing = False
        self._lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static("AGENTS · Loading recorded worker identities…", id="agents-overview-heading")
        with Horizontal(id="agents-overview-body"):
            yield OptionList(id="agents-overview-picker")
            yield AgentOutputText(id="agents-overview-detail")
        yield Static(
            "↑/↓ agents · drag to select · Ctrl+Shift+C copy · T tasks · Esc back",
            id="agents-overview-help",
        )

    async def on_mount(self) -> None:
        self.query_one(OptionList).focus()
        await self.refresh_agents()
        if not self._closing:
            self._timer = self.set_interval(2, self.refresh_agents)

    async def on_unmount(self) -> None:
        self._closing = True
        if self._timer is not None:
            self._timer.stop()

    def action_back(self) -> None:
        self.dismiss(None)

    async def refresh_agents(self) -> None:
        if self._closing or self._lock.locked():
            return
        async with self._lock:
            try:
                assignments = tuple(
                    a
                    for a in await self.backend.list_agents()
                    if a.session_id in (None, self.session_id)
                )
            except Exception as exc:
                if not self._closing:
                    self.query_one("#agents-overview-heading", Static).update(
                        f"AGENTS · Refresh interrupted · showing saved state · {exc}"
                    )
                return
            if self._closing:
                return
            groups: dict[str, list[AgentView]] = {}
            for assignment in assignments:
                key = (
                    assignment.specialist_instance_id
                    if assignment.identity_provenance != "legacy_inferred"
                    and assignment.specialist_instance_id
                    else assignment.owner
                    if assignment.owner != "unassigned"
                    else assignment.assignment_id or assignment.name
                )
                groups.setdefault(key, []).append(assignment)
            self._groups = {key: tuple(rows) for key, rows in groups.items()}
            keys = tuple(self._groups)
            if self._selected not in self._groups:
                self._selected = keys[0] if keys else None
            picker = self.query_one(OptionList)
            with picker.prevent(OptionList.OptionHighlighted):
                picker.clear_options().add_options(
                    [
                        _workspace_specialist_label(rows[0]) + f" · {len(rows)} tasks"
                        for rows in self._groups.values()
                    ]
                )
                picker.highlighted = keys.index(self._selected) if self._selected else None
            self.query_one("#agents-overview-heading", Static).update(
                f"AGENTS · {len(keys)} recorded agent identities · {len(assignments)} tasks"
            )
            self._show_agent(preserve_scroll=True)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        keys = tuple(self._groups)
        if 0 <= event.option_index < len(keys):
            key = keys[event.option_index]
            if key != self._selected:
                self._selected = key
                self._show_agent(preserve_scroll=False)

    def _show_agent(self, *, preserve_scroll: bool) -> None:
        rows = self._groups.get(self._selected or "", ())
        if not rows:
            text = "No agent assignments recorded for this session."
        else:
            current = next(
                (a for a in rows if _agent_display_state(a.latest_event) == "working"),
                next(
                    (a for a in rows if _agent_display_state(a.latest_event) == "queued"), rows[-1]
                ),
            )
            text = (
                f"Agent: {_workspace_specialist_label(current)}\n"
                f"Identity: {self._selected}\nModel: {current.provider}\n"
                f"Activity: {_worker_activity(current)}\n"
                f"Current/latest task: {_workspace_task_label(current)}\n\nTask history\n"
            )
            for task in rows:
                text += (
                    f"{_agent_display_state(task.latest_event)} · {_workspace_task_label(task)}\n"
                )
                if task.result:
                    text += f"  Result: {task.result}\n"
            text += "\nLatest recorded output\n" + "\n\n".join(current.output)
        self.query_one(AgentOutputText).update(text, preserve_scroll=preserve_scroll)


_CAMPAIGN_LEAD_PREFIX = "campaign-lead::"


def _plain_log_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_log_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_log_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_log_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _log_json(row: str) -> tuple[str, Mapping[str, object] | None]:
    if row.strip().startswith("{"):
        prefix = ""
        candidate = row.strip()
    else:
        prefix, separator, body = row.partition(":")
        candidate = body.strip() if separator else row.strip()
    if not candidate.startswith("{"):
        return prefix.strip(), None
    try:
        decoded = json.loads(candidate)
    except (TypeError, ValueError):
        return prefix.strip(), None
    return prefix.strip(), decoded if isinstance(decoded, Mapping) else None


def _bubble(title: str, body: str, width: int) -> str:
    usable = max(28, width - 5)
    safe_title = _short_activity(_scrub_secrets(title.replace("\n", " ")), usable)
    safe_body = _scrub_secrets(body.strip()) or "No detail recorded."
    lines: list[str] = []
    for paragraph in safe_body.splitlines() or [safe_body]:
        lines.extend(
            textwrap.wrap(
                paragraph,
                width=usable,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [""]
        )
    return "\n".join((f"╭─ {safe_title}", *(f"│ {line}" for line in lines), "╰─"))


def _first_log_values(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    found: dict[str, object] = {}

    def visit(item: object) -> None:
        if len(found) == len(keys):
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).casefold()
                if (
                    normalized in keys
                    and normalized not in found
                    and nested not in (None, "", [], {})
                ):
                    found[normalized] = nested
                visit(nested)
        elif isinstance(item, (tuple, list)):
            for nested in item:
                visit(nested)

    visit(value)
    return found


def _tool_call_summary(payload: Mapping[str, object]) -> tuple[str, str]:
    function = payload.get("function")
    function_map = function if isinstance(function, Mapping) else {}
    name = str(function_map.get("name") or "tool")
    arguments: object = function_map.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {}
    safe = _first_log_values(
        arguments,
        ("method", "url", "path", "message", "prompt", "text", "element_ref", "step"),
    )
    detail = "\n".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in safe.items())
    return f"Action · {name}", detail or "Request sent."


def _tool_result_summary(payload: Mapping[str, object]) -> tuple[str, str]:
    kind = str(payload.get("kind") or "response")
    safe = _first_log_values(
        payload,
        (
            "origin",
            "url",
            "status",
            "flags",
            "score_delta",
            "mood",
            "turns_remaining",
            "run_status",
            "outcome",
            "message",
        ),
    )
    if isinstance(payload.get("elements"), (tuple, list)):
        safe["visible_controls"] = len(payload["elements"])
    detail = "\n".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in safe.items())
    return f"Target · {kind}", detail or "Response recorded; open Raw JSON for the full payload."


def _lead_event_summary(payload: Mapping[str, object]) -> str:
    kind = str(payload.get("kind") or "lead update").replace("_", " ")
    parts: list[str] = []
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    plan = payload.get("plan_update")
    if isinstance(plan, Mapping) and isinstance(plan.get("rationale"), str):
        parts.append("Rationale: " + str(plan["rationale"]))
    actions = payload.get("actions")
    if isinstance(actions, (tuple, list)):
        labels = [
            str(item.get("kind") or item.get("action") or item.get("profile_id") or "decision")
            for item in actions
            if isinstance(item, Mapping)
        ]
        parts.append(
            f"Decisions: {len(actions)}" + (" · " + ", ".join(labels[:4]) if labels else "")
        )
    followups = payload.get("followups")
    if isinstance(followups, (tuple, list)):
        labels = [
            str(item.get("profile_id") or item.get("objective") or "follow-up")
            for item in followups
            if isinstance(item, Mapping)
        ]
        parts.append(
            f"Follow-ups dispatched: {len(followups)}"
            + (
                " · " + ", ".join(_short_activity(label, 48) for label in labels[:4])
                if labels
                else ""
            )
        )
    for key in ("blocker", "error", "reason"):
        if payload.get(key):
            parts.append(f"{key.title()}: {payload[key]}")
            break
    if not parts:
        parts.append(kind.capitalize() + ".")
    return "\n".join(parts)


def _readable_log(rows: Iterable[str], *, width: int, lead: bool = False) -> str:
    rendered: list[str] = []
    for raw_row in rows:
        row = str(raw_row)
        prefix, payload = _log_json(row)
        if lead and payload is not None:
            lead_payload = payload.get("payload")
            lead_payload = lead_payload if isinstance(lead_payload, Mapping) else payload
            timestamp = str(payload.get("created_at") or "time unavailable")
            timestamp = timestamp.replace("T", " ").replace("+00:00", "Z")
            wave = payload.get("wave")
            kind = str(lead_payload.get("kind") or "update").replace("_", " ")
            title = f"Lead · {timestamp}" + (f" · Wave {wave}" if wave is not None else "")
            rendered.append(
                _bubble(title, kind.title() + "\n" + _lead_event_summary(lead_payload), width)
            )
        elif prefix.casefold() == "tool call" and payload is not None:
            title, body = _tool_call_summary(payload)
            rendered.append(_bubble(title, body, width))
        elif prefix.casefold() == "tool" and payload is not None:
            title, body = _tool_result_summary(payload)
            rendered.append(_bubble(title, body, width))
        elif prefix.casefold() == "assistant":
            rendered.append(_bubble("Worker reasoning", row.partition(":")[2], width))
        elif prefix.casefold() in {"result", "assessment"}:
            rendered.append(_bubble("Assessment", row.partition(":")[2], width))
        elif prefix.casefold() in {"failure", "error"}:
            rendered.append(_bubble("Blocker", row.partition(":")[2], width))
        elif row.casefold().startswith("attempt "):
            rendered.append("── " + _scrub_secrets(row) + " ──")
        else:
            rendered.append(_bubble(prefix or "Event", row.partition(":")[2] or row, width))
    return "\n\n".join(rendered)


class AgentScreen(ModalScreen[None]):
    """Full-screen worker workspace with independent live panes and retained transcripts."""

    DEFAULT_CSS = """
    AgentScreen { background: $background; }
    AgentScreen #agent-workspace { width: 100%; height: 100%; padding: 0 1; }
    AgentScreen #agent-heading { height: 1; padding: 0 1; text-style: bold; }
    AgentScreen #agent-status { height: 1; padding: 0 1; color: $text-muted; }
    AgentScreen #agent-body { height: 1fr; }
    AgentScreen #agent-sidebar {
        width: 30%; min-width: 24; max-width: 42; border-right: solid $panel;
    }
    AgentScreen #agent-picker { height: 1fr; border: none; padding: 0; }
    AgentScreen #agent-picker > .option-list--option-highlighted {
        color: $foreground; background: $boost; text-style: bold;
    }
    AgentScreen #agent-panes { width: 1fr; padding-left: 1; }
    AgentScreen .worker-pane {
        height: 1fr; border: solid $panel; margin-bottom: 1; overflow: hidden hidden;
    }
    AgentScreen .worker-pane.selected { border: solid $accent; }
    AgentScreen .worker-title { height: 1; padding: 0 1; background: $surface; }
    AgentScreen .worker-output {
        height: 1fr; padding: 0 1; overflow-x: hidden;
        overflow-y: scroll; scrollbar-gutter: stable;
    }
    AgentScreen .worker-pane.hidden { display: none; }
    AgentScreen #agent-help { height: 1; color: $text-muted; }
    AgentScreen #agent-workspace.expanded #agent-sidebar { display: none; }
    AgentScreen #agent-workspace.expanded #agent-panes { padding-left: 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Back", show=False, priority=True),
        Binding("up", "previous_agent", "Previous worker", show=False, priority=True),
        Binding("down", "next_agent", "Next worker", show=False, priority=True),
        Binding("tab", "next_pane", "Next pane", show=False, priority=True),
        Binding("shift+tab", "previous_pane", "Previous pane", show=False, priority=True),
        Binding("enter", "inspect_agent", "Expand transcript", show=False, priority=True),
        Binding("space", "pin_agent", "Pin pane", show=False, priority=True),
        Binding("a", "agents_overview", "Agent overview", show=False, priority=True),
        Binding("r", "toggle_raw", "Toggle raw log", show=False, priority=True),
    ]

    def __init__(self, backend: ChatBackend, session_id: str) -> None:
        super().__init__()
        self.backend = backend
        self.session_id = session_id
        self._refresh_lock = asyncio.Lock()
        self._refresh_timer: Any | None = None
        self._closing = False
        self._agents: tuple[AgentView, ...] = ()
        self._campaigns: tuple[CampaignView, ...] = ()
        self._selected_agent_key: str | None = None
        self._expanded = False
        self._picker_keys: tuple[str, ...] = ()
        self._picker_labels: tuple[str, ...] = ()
        self._pane_keys: tuple[str, ...] = ()
        self._pinned: list[str] = []
        self._fingerprints: dict[str, tuple[object, ...]] = {}
        self._unread: dict[str, int] = {}
        self._transcript_key: str | None = None
        self._transcript: tuple[str, ...] | None = None
        self._transcript_error = ""
        self._rendered_keys: dict[int, str] = {}
        self._raw_logs = False

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-workspace"):
            yield Static("AGENT WORKSPACE", id="agent-heading", markup=False)
            yield Static("Loading campaign…", id="agent-status", markup=False)
            with Horizontal(id="agent-body"):
                with Vertical(id="agent-sidebar"):
                    yield OptionList(id="agent-picker")
                with Vertical(id="agent-panes"):
                    for index in range(3):
                        with Vertical(id=f"worker-pane-{index}", classes="worker-pane"):
                            yield Static(
                                "", id=f"worker-title-{index}", classes="worker-title", markup=False
                            )
                            yield AgentOutputText(
                                id="overview-content" if index == 0 else f"worker-output-{index}",
                                classes="worker-output",
                            )
            yield Static(
                "↑/↓ select · Tab pane · Space pin · Enter expand · PgUp/Dn scroll · "
                "A agents · drag to select · Ctrl+Shift+C copy · Esc back",
                id="agent-help",
                markup=False,
            )

    async def on_mount(self) -> None:
        await self.refresh_progress()
        self.query_one("#agent-picker", OptionList).focus()
        if not self._closing:
            self._refresh_timer = self.set_interval(2.0, self.refresh_progress)

    async def refresh_progress(self) -> None:
        if self._closing or self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            try:
                agents, campaigns = await asyncio.gather(
                    self.backend.list_agents(), self.backend.list_campaigns()
                )
                if self._closing:
                    return
                self._campaigns = _campaigns_for_session(campaigns, self.session_id)
                visible = _current_campaigns(self._campaigns) or self._campaigns[:1]
                incoming = _ordered_agents(_agents_for_campaigns(agents, visible))
                if visible:
                    campaign = visible[0]
                    lead_rows: tuple[object, ...] = ()
                    lead_loader = getattr(self.backend, "get_campaign_lead_log", None)
                    if callable(lead_loader):
                        try:
                            lead_rows = tuple(await lead_loader(campaign.id, 100))
                        except Exception:
                            # An older daemon may not expose the read-only Lead log yet.
                            lead_rows = ()
                    serialized = tuple(
                        json.dumps(
                            _redact_sensitive(_plain_log_value(row)), sort_keys=True, default=str
                        )
                        for row in lead_rows
                    )
                    latest_payload = _plain_log_value(lead_rows[-1]) if lead_rows else {}
                    latest_payload = latest_payload if isinstance(latest_payload, Mapping) else {}
                    nested_payload = latest_payload.get("payload")
                    nested_payload = nested_payload if isinstance(nested_payload, Mapping) else {}
                    incoming = (
                        *incoming,
                        AgentView(
                            name="campaign-lead",
                            owner="campaign",
                            task="Coordinate workers, review evidence, and decide the next wave",
                            provider="Campaign Lead",
                            elapsed="live journal",
                            latest_event=(
                                "working · "
                                + str(nested_payload.get("kind") or "reviewing campaign")
                                if campaign.status == "executing"
                                else campaign.status
                            ),
                            result=None,
                            profile="Campaign Lead",
                            technique="coordination",
                            campaign_id=campaign.id,
                            assignment_id=_CAMPAIGN_LEAD_PREFIX + campaign.id,
                            identity_provenance="durable",
                            execution_stage="coordinate",
                            activity=(
                                str(nested_payload.get("kind") or "reviewing campaign").replace(
                                    "_", " "
                                ),
                            ),
                            output=serialized,
                        ),
                    )
                # Keep rows in place as states change; new assignments append.
                by_key = {self._key(agent): agent for agent in incoming}
                old_keys = tuple(self._key(agent) for agent in self._agents)
                keys = tuple(key for key in old_keys if key in by_key)
                keys += tuple(key for key in by_key if key not in keys)
                self._agents = tuple(by_key[key] for key in keys)
                for agent in self._agents:
                    key = self._key(agent)
                    fingerprint = (agent.output, agent.result, agent.latest_event)
                    if key in self._fingerprints and self._fingerprints[key] != fingerprint:
                        self._unread[key] = self._unread.get(key, 0) + 1
                    self._fingerprints[key] = fingerprint
                if self._selected_agent_key not in keys:
                    self._selected_agent_key = keys[0] if keys else None
                self._pinned = [key for key in self._pinned if key in keys]
                self._render_progress(reveal_cursor=True)
                if self._expanded:
                    await self._refresh_transcript()
                working = sum(
                    _agent_display_state(a.latest_event) == "working"
                    for a in self._agents
                    if a.profile != "Campaign Lead"
                )
                specialist_ids = {
                    a.specialist_instance_id or a.name
                    for a in self._agents
                    if a.profile not in {"Unassigned/structural tasks", "Campaign Lead"}
                }
                workers = tuple(a for a in self._agents if a.profile != "Campaign Lead")
                self.query_one("#agent-status", Static).update(
                    f"{len(workers)} tasks · "
                    f"{len({a.owner for a in workers if a.owner != 'unassigned'})} workers"
                    f" · {len(specialist_ids)} specialists"
                    f" · {working} working"
                    + (
                        f" · {_campaign_display_status(visible[0].status)} · saved history"
                        if visible and visible[0].status in {"failed", "completed", "cancelled"}
                        else " · Live · refreshed every 2s"
                    )
                )
            except Exception as exc:
                if not self._closing:
                    self.query_one("#agent-status", Static).update(
                        f"Refresh failed · showing last recorded state · {exc}"
                    )

    async def on_unmount(self) -> None:
        self._closing = True
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def action_agents_overview(self) -> None:
        self.app.push_screen(AgentsOverviewScreen(self.backend, self.session_id))

    def action_toggle_raw(self) -> None:
        self._raw_logs = not self._raw_logs
        self._render_progress(reveal_cursor=False)

    def on_resize(self) -> None:
        if self.is_mounted and self.query("#agent-picker"):
            self.call_after_refresh(self._render_progress, reveal_cursor=False)

    @staticmethod
    def _key(agent: AgentView) -> str:
        return agent.assignment_id or agent.name

    def _move_agent(self, offset: int) -> None:
        keys = tuple(self._key(agent) for agent in self._agents)
        if not keys:
            return
        current = keys.index(self._selected_agent_key) if self._selected_agent_key in keys else 0
        self._selected_agent_key = keys[(current + offset) % len(keys)]
        self._transcript = None
        self._render_progress(reveal_cursor=True)
        if self._expanded:
            self.run_worker(self._refresh_transcript(), group="transcript", exclusive=True)

    def action_previous_agent(self) -> None:
        self._move_agent(-1)

    def action_next_agent(self) -> None:
        self._move_agent(1)

    def _move_pane(self, offset: int) -> None:
        if self._expanded or not self._pane_keys:
            self._move_agent(offset)
            return
        current = (
            self._pane_keys.index(self._selected_agent_key)
            if self._selected_agent_key in self._pane_keys
            else 0
        )
        self._selected_agent_key = self._pane_keys[(current + offset) % len(self._pane_keys)]
        self._render_progress(reveal_cursor=True)
        index = self._pane_keys.index(self._selected_agent_key)
        self.query_one(
            "#overview-content" if index == 0 else f"#worker-output-{index}", AgentOutputText
        ).focus()

    def action_next_pane(self) -> None:
        self._move_pane(1)

    def action_previous_pane(self) -> None:
        self._move_pane(-1)

    def action_pin_agent(self) -> None:
        key = self._selected_agent_key
        if key is None or self._expanded:
            return
        if key in self._pinned:
            self._pinned.remove(key)
        else:
            self._pinned.append(key)
            self._pinned = self._pinned[-2:]
        self._render_progress(reveal_cursor=False)

    def action_inspect_agent(self) -> None:
        if self._selected_agent_key is None:
            return
        for view in self.query(AgentOutputText):
            view.move_cursor((0, 0))
        self._expanded = True
        self._transcript = None
        self._transcript_error = ""
        self._render_progress(reveal_cursor=False)
        self.query_one("#overview-content", AgentOutputText).focus()
        self.run_worker(self._refresh_transcript(), group="transcript", exclusive=True)

    def action_cancel(self) -> None:
        if self._expanded:
            for view in self.query(AgentOutputText):
                view.move_cursor((0, 0))
            self._expanded = False
            self._render_progress(reveal_cursor=True)
            self.query_one("#agent-picker", OptionList).focus()
        else:
            self.dismiss(None)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option_index != self.query_one("#agent-picker", OptionList).highlighted:
            return
        if event.option_index < len(self._picker_keys):
            key = self._picker_keys[event.option_index]
            if key != self._selected_agent_key:
                self._selected_agent_key = key
                self._render_progress(reveal_cursor=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_index < len(self._picker_keys):
            self._selected_agent_key = self._picker_keys[event.option_index]
            self.action_inspect_agent()

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        while widget is not None:
            if widget.id and widget.id.startswith("worker-pane-"):
                index = int(widget.id.rsplit("-", 1)[1])
                if index < len(self._pane_keys):
                    self._selected_agent_key = self._pane_keys[index]
                    self._render_progress(reveal_cursor=True)
                return
            widget = widget.parent

    async def _refresh_transcript(self) -> None:
        key = self._selected_agent_key
        agent = next((a for a in self._agents if self._key(a) == key), None)
        if agent is None:
            return
        is_lead = key.startswith(_CAMPAIGN_LEAD_PREFIX) if key else False
        loader = getattr(
            self.backend,
            "get_campaign_lead_log" if is_lead else "get_agent_transcript",
            None,
        )
        try:
            if callable(loader) and agent.assignment_id:
                loaded = (
                    await loader(agent.campaign_id, 100)
                    if is_lead
                    else await loader(agent.assignment_id)
                )
                transcript = (
                    tuple(
                        json.dumps(
                            _redact_sensitive(_plain_log_value(row)), sort_keys=True, default=str
                        )
                        for row in loaded
                    )
                    if is_lead
                    else tuple(loaded)
                )
            else:
                transcript = ()
            error = (
                ""
                if transcript
                else (
                    "No Campaign Lead journal entries recorded yet."
                    if is_lead
                    else "Only recent output is available for this worker."
                )
            )
        except Exception as exc:
            transcript = ()
            error = f"Could not load retained transcript: {exc}"
        if self._closing or not self._expanded or key != self._selected_agent_key:
            return
        self._transcript_key = key
        self._transcript = transcript
        self._transcript_error = error
        self._render_progress(reveal_cursor=False)

    def _render_progress(self, *, reveal_cursor: bool) -> None:
        keys = tuple(self._key(agent) for agent in self._agents)
        selected = self._selected_agent_key
        selected_index = keys.index(selected) if selected in keys else 0
        if self._expanded:
            pane_keys = (selected,) if selected else ()
        else:
            candidates = list(self._pinned)
            # Keep the same pane set while switching focus within it.
            if selected in self._pane_keys and not self._pinned:
                candidates.extend(key for key in self._pane_keys if key in keys)
            else:
                candidates.extend(keys[selected_index : selected_index + 3])
            if selected and selected not in candidates:
                candidates.append(selected)
            candidates.extend(keys[selected_index : selected_index + 3])
            slots = 3 if self.size.height >= 36 else 2 if self.size.height >= 28 else 1
            # A compact terminal always prioritizes the selected worker over pinned panes.
            if selected and selected not in tuple(dict.fromkeys(candidates))[:slots]:
                candidates = [selected, *candidates]
            pane_keys = tuple(dict.fromkeys(candidates))[:slots]
        self._pane_keys = pane_keys
        for key in pane_keys:
            self._unread[key] = 0
        labels = tuple(self._picker_label(agent, index) for index, agent in enumerate(self._agents))
        picker = self.query_one("#agent-picker", OptionList)
        if keys != self._picker_keys or labels != self._picker_labels:
            self._picker_keys, self._picker_labels = keys, labels
            with picker.prevent(OptionList.OptionHighlighted):
                picker.clear_options().add_options(labels)
                picker.highlighted = selected_index if keys else None
        else:
            with picker.prevent(OptionList.OptionHighlighted):
                picker.highlighted = selected_index if keys else None
        self.query_one("#agent-workspace").set_class(self._expanded, "expanded")
        objective = self._campaigns[0].objective if self._campaigns else "No campaign selected"
        wave_label = _campaign_wave_label(self._campaigns[0]) if self._campaigns else ""
        self.query_one("#agent-heading", Static).update(
            "AGENT WORKSPACE · " + (wave_label + " · " if wave_label else "") + objective
        )
        self.query_one("#agent-help", Static).update(
            "↑/↓ entries · PgUp/PgDn scroll · R readable/raw · "
            "Esc workspace · Esc again conversation"
            if self._expanded
            else "↑/↓ select · Tab pane · Space pin · Enter expand · PgUp/Dn scroll · "
            "R readable/raw · A agents · drag to select · Ctrl+Shift+C copy · Esc back"
        )
        by_key = {self._key(agent): agent for agent in self._agents}
        for index in range(3):
            pane = self.query_one(f"#worker-pane-{index}")
            key = pane_keys[index] if index < len(pane_keys) else None
            pane.set_class(key is None and (bool(keys) or index > 0), "hidden")
            pane.set_class(key == selected and key is not None, "selected")
            view = self.query_one(
                "#overview-content" if index == 0 else f"#worker-output-{index}", AgentOutputText
            )
            if key is None:
                content = Text("No workers recorded for this session. Start or resume a campaign.")
                title = "No worker selected"
            else:
                agent = by_key[key]
                is_lead = agent.profile == "Campaign Lead"
                state = _agent_display_state(agent.latest_event)
                glyph, style = _agent_state_chip(state)
                stage = agent.execution_stage or _agent_stage(agent)
                worker_number = sum(
                    item.profile != "Campaign Lead" for item in self._agents[: keys.index(key) + 1]
                )
                title = _short_activity(
                    (
                        f"{glyph} Campaign Lead · {state} · {stage}"
                        if is_lead
                        else f"{glyph} {worker_number:02d} · "
                        f"{state} · {_workspace_specialist_label(agent)} · {stage}"
                    ),
                    max(20, self.size.width - self.query_one("#agent-sidebar").size.width - 8),
                )
                content = Text()
                content.append(
                    (
                        "Campaign Lead\n"
                        if is_lead
                        else f"Agent: {_workspace_specialist_label(agent)}\n"
                    ),
                    style="cyan",
                )
                if not is_lead:
                    content.append(f"Worker owner: {agent.owner}\n", style="dim")
                content.append("Task: " + _workspace_task_label(agent) + "\n", style="bold")
                content.append(
                    _worker_activity(agent) + "\n", style="cyan" if state == "working" else style
                )
                content.append(f"{agent.provider} · {agent.elapsed}\n\n", style="dim")
                if state == "failed":
                    content.append(
                        "Failure: " + (agent.result or "No reason recorded") + "\n\n",
                        style="#ff8290",
                    )
                if self._expanded:
                    content.append(
                        ("Role: Campaign Lead\n" if is_lead else f"Worker: {agent.name}\n"),
                        style="dim",
                    )
                    if agent.technique:
                        content.append(f"Technique: {agent.technique}\n", style="dim")
                    content.append("Full assignment\n", style="bold")
                    content.append(agent.task + "\n\n")
                    content.append("Recorded activity\n", style="bold")
                    content.append("\n".join(agent.activity) + "\n\n")
                    content.append(
                        ("Campaign Lead journal\n" if is_lead else "Retained worker transcript\n"),
                        style="bold cyan",
                    )
                    if self._transcript is None:
                        content.append("Loading transcript…\n", style="dim")
                    elif self._transcript_error:
                        content.append(self._transcript_error + "\n", style="yellow")
                output = (
                    self._transcript
                    if self._expanded and self._transcript_key == key and self._transcript
                    else agent.output
                )
                if output:
                    rows = tuple(str(row) for row in output)
                    if self._raw_logs:
                        content.append("\n\n".join(_scrub_secrets(row) for row in rows) + "\n\n")
                    else:
                        pane_width = max(
                            32,
                            self.size.width
                            - (0 if self._expanded else self.query_one("#agent-sidebar").size.width)
                            - 10,
                        )
                        content.append(_readable_log(rows, width=pane_width, lead=is_lead) + "\n\n")
                if not output:
                    content.append(agent.result or "No worker output recorded yet.", style="dim")
            self.query_one(f"#worker-title-{index}", Static).update(
                Text(title, style=style if key is not None else "cyan")
            )
            if view.plain != content.plain or self._rendered_keys.get(index) != key:
                view.update(content, preserve_scroll=self._rendered_keys.get(index) == key)
            if key:
                self._rendered_keys[index] = key
        if reveal_cursor and not self._expanded:
            picker.scroll_to_highlight()

    def _picker_label(self, agent: AgentView, index: int) -> Text:
        key = self._key(agent)
        state = _agent_display_state(agent.latest_event)
        glyph, style = _agent_state_chip(state)
        sidebar_width = self.query_one("#agent-sidebar").size.width
        if sidebar_width < 24:
            sidebar_width = min(42, max(24, int(self.size.width * 0.3)))
        width = max(12, sidebar_width - 3)
        label = Text()
        cursor = "›" if key == self._selected_agent_key else " "
        suffix = " P" if key in self._pinned else ""
        unread = self._unread.get(key, 0)
        if unread:
            suffix += f" +{unread}"
        is_lead = agent.profile == "Campaign Lead"
        worker_index = sum(item.profile != "Campaign Lead" for item in self._agents[: index + 1])
        label.append(
            f"{cursor}{glyph} " + ("LEAD " if is_lead else f"{worker_index:02d} "),
            style=style,
        )
        label.append(
            _short_activity(_workspace_specialist_label(agent), max(4, width - 7 - len(suffix))),
            style="bold",
        )
        label.append(suffix, style="yellow" if unread else "cyan")
        label.append("\n" + _short_activity(_workspace_task_label(agent), width))
        label.append(
            "\n" + _short_activity(state + " · " + _worker_activity(agent), width),
            style="" if key == self._selected_agent_key else "dim",
        )
        return label


class ResumeScreen(ModalScreen[str | None]):
    DEFAULT_CSS = (
        ChoiceScreen.DEFAULT_CSS.replace("ChoiceScreen", "ResumeScreen")
        + """
    ResumeScreen #dialog { max-height: 90%; overflow-y: auto; }
    ResumeScreen #dialog Button { height: auto; min-height: 3; text-align: left; }
    """
    )
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "next_session", "Next session", show=False),
        Binding("up", "previous_session", "Previous session", show=False),
    ]

    def on_mount(self) -> None:
        self.query(Button).first().focus()

    def action_next_session(self) -> None:
        self.focus_next()

    def action_previous_session(self) -> None:
        self.focus_previous()

    def __init__(self, sessions: tuple[ResumeSessionView, ...]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Resume a durable session")
            if not self.sessions:
                yield Static("No resumable sessions found.", markup=False)
            for index, session in enumerate(self.sessions):
                suffix = f" · {session.updated_at}" if session.updated_at else ""
                yield Button(
                    f"{session.title} · {session.id}{suffix}", id=f"resume-session-{index}"
                )
            yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("resume-session-"):
            index = int(event.button.id.removeprefix("resume-session-"))
            self.dismiss(self.sessions[index].id)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionBrowserScreen(ModalScreen[str | None]):
    """Keyboard-first searchable page of durable sessions."""

    DEFAULT_CSS = """
    SessionBrowserScreen { align: center middle; background: $background 70%; }
    #dialog {
        width: 72; max-width: 92%; height: 90%; border: round $accent;
        padding: 1 2; background: $surface;
    }
    #dialog Label { margin-bottom: 1; text-style: bold; }
    #dialog Button, #dialog Input { width: 100%; }
    #session-help { margin-bottom: 1; color: $text-muted; }
    #session-search { margin-bottom: 1; }
    #session-search-submit { margin-bottom: 1; }
    #session-results { height: 1fr; margin-top: 1; }
    #session-results Button { height: auto; min-height: 3; text-align: left; }
    #session-previous, #session-next, #session-close { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Close", show=False)]

    def __init__(self, page: ResumeSessionPage) -> None:
        super().__init__()
        self.page = page

    def compose(self) -> ComposeResult:
        start = self.page.offset + 1 if self.page.items else 0
        end = self.page.offset + len(self.page.items)
        with Vertical(id="dialog"):
            yield Label(f"Sessions {start}-{end} of {self.page.total}")
            yield Static(
                "↑/↓ select · Enter resume · / search · 1–9 quick resume · "
                "PgUp/PgDn pages · Esc close",
                id="session-help",
                markup=False,
            )
            yield Input(
                value=self.page.query,
                placeholder="Search title or session ID",
                name="Search durable sessions",
                id="session-search",
            )
            yield Button("Search", id="session-search-submit")
            with VerticalScroll(id="session-results"):
                if not self.page.items:
                    yield Static("No matching sessions.", markup=False)
                for index, session in enumerate(self.page.items):
                    suffix = f" · {session.updated_at}" if session.updated_at else ""
                    yield Button(
                        f"{index + 1}. {session.title} · {session.id}{suffix}",
                        id=f"browse-session-{index}",
                    )
            yield Button("Previous", id="session-previous", disabled=self.page.offset == 0)
            yield Button(
                "Next",
                id="session-next",
                disabled=self.page.offset + len(self.page.items) >= self.page.total,
            )
            yield Button("Close", id="session-close")

    def on_mount(self) -> None:
        session_buttons = self._session_buttons()
        if session_buttons:
            session_buttons[0].focus()
        else:
            self.query_one("#session-search", Input).focus()

    def on_key(self, event: events.Key) -> None:
        """Keep browsing keyboard-first without breaking normal search editing."""
        if isinstance(self.focused, Input):
            return

        if event.key == "slash":
            event.prevent_default()
            event.stop()
            self.query_one("#session-search", Input).focus()
            return

        if event.key in {"up", "k"}:
            self._move_session(-1)
        elif event.key in {"down", "j"}:
            self._move_session(1)
        elif event.key == "home":
            self._focus_session(0)
        elif event.key == "end":
            self._focus_session(len(self.page.items) - 1)
        elif event.key in {"pageup", "p"} and self.page.offset > 0:
            self.dismiss("previous")
        elif (
            event.key in {"pagedown", "n"}
            and self.page.offset + len(self.page.items) < self.page.total
        ):
            self.dismiss("next")
        elif event.key.isdigit() and 1 <= int(event.key) <= min(9, len(self.page.items)):
            self.dismiss(f"resume:{self.page.items[int(event.key) - 1].id}")
        else:
            return

        event.prevent_default()
        event.stop()

    def _session_buttons(self) -> list[Button]:
        return list(self.query("#session-results Button"))

    def _focus_session(self, index: int) -> None:
        buttons = self._session_buttons()
        if buttons:
            buttons[max(0, min(index, len(buttons) - 1))].focus()

    def _move_session(self, offset: int) -> None:
        buttons = self._session_buttons()
        if not buttons:
            return
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if identifier.startswith("browse-session-"):
            index = int(identifier.removeprefix("browse-session-"))
            self.dismiss(f"resume:{self.page.items[index].id}")
        elif identifier == "session-next":
            self.dismiss("next")
        elif identifier == "session-previous":
            self.dismiss("previous")
        elif identifier == "session-search-submit":
            self.dismiss(f"search:{self.query_one('#session-search', Input).value}")
        else:
            self.dismiss(None)

    @on(Input.Submitted, "#session-search")
    def submit_search(self, event: Input.Submitted) -> None:
        self.dismiss(f"search:{event.value}")

    def action_cancel(self) -> None:
        self.dismiss(None)


class StartupSessionScreen(ModalScreen[str]):
    """Borderless startup gate that never reveals history before consent."""

    class RetryRequested(Message):
        """Retry history without choosing or creating a session."""

    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("1", "resume", "Resume", show=False, priority=True),
        Binding("2", "new_session", "New session", show=False, priority=True),
        Binding("3", "browse_sessions", "Browse sessions", show=False, priority=True),
        Binding("r", "retry_history", "Retry history", show=False, priority=True),
        Binding("escape", "new_session", "New session", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    StartupSessionScreen {
        align: center middle; background: transparent; padding: 1;
    }
    #startup-shell {
        width: 100%; max-width: 120; height: auto;
    }
    #startup-logo {
        width: 100%; height: auto; margin-bottom: 2;
        content-align: center middle; color: cyan;
    }
    #startup-session {
        width: 100%; height: auto; padding: 1 2;
        background: #202020;
    }
    #startup-title { text-style: bold; color: #d7d7d7; margin-bottom: 1; }
    #startup-detail { color: #8a8a8a; margin-bottom: 1; }
    #startup-session Button {
        width: 100%; height: 1; min-height: 1; margin: 0; padding: 0 1;
        border: none; background: #202020; color: #bcbcbc; text-align: left;
    }
    #startup-session Button:focus { background: #153047; color: #82e9f5; text-style: bold; }
    #startup-help { color: #666666; margin-top: 1; }
    """

    def __init__(self, sessions: tuple[ResumeSessionView, ...]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        latest = self.sessions[0] if self.sessions else None
        updated = latest.updated_at or "unknown time" if latest else ""
        with Vertical(id="startup-shell"):
            yield Static(
                Text(
                    "\n".join((*WORDMARK, "security evaluation harness for AI systems")),
                    justify="center",
                ),
                id="startup-logo",
            )
            with Vertical(id="startup-session"):
                yield Static(
                    (
                        f"Showing {len(self.sessions)} recent session"
                        f"{'s' if len(self.sessions) != 1 else ''}."
                        if self.sessions
                        else "Loading previous sessions…"
                    ),
                    id="startup-title",
                    markup=False,
                )
                yield Static(
                    (
                        f"Last session: {updated}\n{latest.title}"
                        if latest
                        else "You can start a new session while history loads."
                    ),
                    id="startup-detail",
                    markup=False,
                )
                yield Button(
                    "1. Resume last session", id="startup-resume", disabled=not self.sessions
                )
                yield Button("2. New session  [default]", id="startup-new")
                yield Button(
                    "3. Browse previous sessions…", id="startup-browse", disabled=not self.sessions
                )
                yield Static(
                    "Enter starts a new session · 1 resumes · 3 browses recent history",
                    id="startup-help",
                    markup=False,
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "startup-browse" and not self.sessions:
            self.action_retry_history()
            return
        choices = {
            "startup-resume": "resume",
            "startup-new": "new",
            "startup-browse": "browse",
        }
        self.dismiss(choices.get(event.button.id, "new"))

    def on_mount(self) -> None:
        self.query_one("#startup-new", Button).focus()

    def action_resume(self) -> None:
        if self.sessions:
            self.dismiss("resume")

    def action_new_session(self) -> None:
        self.dismiss("new")

    def action_browse_sessions(self) -> None:
        if self.sessions:
            self.dismiss("browse")
        else:
            self.action_retry_history()

    def action_retry_history(self) -> None:
        if not self.sessions and not self.query_one("#startup-browse", Button).disabled:
            self.post_message(self.RetryRequested())

    def show_history_error(self) -> None:
        self.query_one("#startup-title", Static).update("Could not load previous sessions")
        self.query_one("#startup-detail", Static).update(
            "History is unavailable; your saved sessions have not been removed.\n"
            "Retry to resume, or choose New session."
        )
        retry = self.query_one("#startup-browse", Button)
        retry.label = "3. Retry loading previous sessions"
        retry.disabled = False
        self.query_one("#startup-help", Static).update(
            "R or 3 retries · Enter starts a new session"
        )

    def show_sessions(self, sessions: tuple[ResumeSessionView, ...]) -> None:
        self.sessions = sessions
        latest = sessions[0]
        self.query_one("#startup-title", Static).update(
            f"Showing {len(sessions)} recent session{'s' if len(sessions) != 1 else ''}."
        )
        self.query_one("#startup-detail", Static).update(
            f"Last session: {latest.updated_at or 'unknown time'}\n{latest.title}"
        )
        self.query_one("#startup-resume", Button).disabled = False
        browse = self.query_one("#startup-browse", Button)
        browse.label = "3. Browse previous sessions…"
        browse.disabled = False
        self.query_one("#startup-help", Static).update(
            "Enter starts a new session · 1 resumes · 3 browses recent history"
        )

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, offset: int) -> None:
        buttons = list(self.query(Button))
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()


class BrowserScreen(OverviewScreen):
    def __init__(
        self,
        contexts: tuple[BrowserContextView, ...],
        takeover: Callable[[str], Awaitable[BrowserContextView]],
    ) -> None:
        super().__init__("Browser contexts", _render_browser_contexts(contexts))
        self.contexts = contexts
        self.takeover = takeover

    def compose(self) -> ComposeResult:
        with Vertical(id="overview"):
            yield Label(self.heading, id="overview-title")
            yield SelectableDrawerText(self.content, id="overview-content")
            for index, context in enumerate(self.contexts):
                if context.owner != "operator":
                    yield Button(f"Take over {context.id}", id=f"browser-takeover-{index}")
            yield Button("Close", id="overview-close")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("browser-takeover-"):
            event.stop()
            index = int(event.button.id.removeprefix("browser-takeover-"))
            updated = await self.takeover(self.contexts[index].id)
            self.contexts = tuple(
                updated if item.id == updated.id else item for item in self.contexts
            )
            self.query_one("#overview-content", SelectableDrawerText).update(
                _render_browser_contexts(self.contexts)
            )
            event.button.disabled = True
            event.button.label = "Operator owns context"
            return
        self.dismiss(None)


class FindingsScreen(OverviewScreen):
    """Finding summary with navigable evidence references."""

    def __init__(self, findings: tuple[FindingView, ...]) -> None:
        super().__init__("Findings", _render_findings(findings))
        self.evidence = tuple(ref for finding in findings for ref in finding.evidence)

    def compose(self) -> ComposeResult:
        with Vertical(id="overview"):
            yield Label(self.heading, id="overview-title")
            yield SelectableDrawerText(self.content, id="overview-content")
            for index, reference in enumerate(self.evidence):
                yield Button(f"Open evidence: {reference}", id=f"evidence-{index}")
            yield Button("Close", id="overview-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        if event.button.id and event.button.id.startswith("evidence-"):
            index = int(event.button.id.removeprefix("evidence-"))
            self.app.push_screen(OverviewScreen("Evidence", self.evidence[index]))
            return
        self.dismiss(None)


class CampaignScreen(OverviewScreen):
    def __init__(
        self,
        campaigns: tuple[CampaignView, ...],
        cancel: Callable[[str], Awaitable[int]],
    ) -> None:
        super().__init__("Campaigns · current session", _render_campaigns(campaigns))
        self.campaigns = campaigns
        self.cancel = cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="overview"):
            yield Label(self.heading, id="overview-title")
            yield SelectableDrawerText(self.content, id="overview-content")
            for index, campaign in enumerate(self.campaigns):
                if campaign.status in {"prepared", "executing", "paused"}:
                    yield Button(f"Stop {campaign.id}", id=f"campaign-stop-{index}")
            if any(item.status in {"prepared", "executing", "paused"} for item in self.campaigns):
                yield Button(
                    "Stop all active campaigns in this session",
                    variant="error",
                    id="campaign-stop-all",
                )
            yield Button("Close", id="overview-close")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        if event.button.id == "campaign-stop-all":
            count = await self.cancel("*")
            self.dismiss(None)
            self.app.notify(f"Stopped {count} campaign(s) in this session")
            return
        if event.button.id and event.button.id.startswith("campaign-stop-"):
            index = int(event.button.id.removeprefix("campaign-stop-"))
            count = await self.cancel(self.campaigns[index].id)
            self.dismiss(None)
            self.app.notify(f"Stopped {count} campaign(s)")
            return
        self.dismiss(None)


class ReportScreen(ModalScreen[None]):
    """Browse saved campaign artifacts and export the selected durable report."""

    DEFAULT_CSS = """
    ReportScreen { background: $background; }
    #report-workspace { width: 100%; height: 100%; padding: 1 2; }
    #report-body { height: 1fr; }
    #report-picker { width: 35%; height: 100%; margin-right: 2; }
    #report-details { width: 1fr; height: 100%; }
    #report-actions { height: auto; layout: grid; grid-size: 3; grid-gutter: 1; }
    #report-actions Button { width: 100%; }
    #report-feedback { height: auto; margin: 1 0; }
    #overview-close { dock: bottom; }
    """
    BINDINGS = [Binding("escape", "close", "Back"), Binding("enter", "open_html", "Open HTML")]

    def __init__(self, backend: ChatBackend, reports: tuple[ReportView, ...]) -> None:
        super().__init__()
        self.backend = backend
        self.reports = list(reports)
        self.runs = tuple(dict.fromkeys(report.run for report in reports))
        self.selected_run = self.runs[0] if self.runs else None
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="report-workspace"):
            yield Label("Reports · select a campaign, then open or export", id="overview-title")
            with Horizontal(id="report-body"):
                yield OptionList(
                    *(
                        f"{index + 1:02} · Campaign {run.removeprefix('campaign/')[:12]}"
                        for index, run in enumerate(self.runs)
                    ),
                    id="report-picker",
                )
                with VerticalScroll(id="report-details"):
                    yield Static("", id="overview-content", markup=False)
            yield Static("", id="report-feedback", markup=False)
            with Vertical(id="report-actions"):
                yield Button("Open HTML", id="report-open-html")
                yield Button("Copy HTML path", id="report-copy-path")
                yield Button("Export Markdown", id="report-export-markdown")
                yield Button("Export JSON", id="report-export-json")
                yield Button("Export CSV bundle", id="report-export-csv")
            yield Button("Close", id="overview-close")

    def on_mount(self) -> None:
        self.query_one("#report-picker", OptionList).focus()
        self._update_selected()

    def _source(self) -> ReportView | None:
        candidates = [item for item in self.reports if item.run == self.selected_run]
        return next(
            (item for item in reversed(candidates) if item.format == "durable-revision"),
            candidates[-1] if candidates else None,
        )

    def _update_selected(self) -> None:
        report = self._source()
        self.query_one("#report-picker", OptionList).disabled = self.busy
        for button in self.query("#report-actions Button"):
            button.disabled = report is None or self.busy
        if report is None:
            self.query_one("#overview-content", Static).update("No saved reports yet.")
            return
        html = report.output.endswith(".html")
        self.query_one("#report-open-html", Button).disabled = not html or self.busy
        self.query_one("#report-copy-path", Button).disabled = not html or self.busy
        for format in ("markdown", "json", "csv"):
            self.query_one(f"#report-export-{format}", Button).disabled = (
                report.format != "durable-revision" or not html or self.busy
            )
        source_label = "HTML" if html else report.format.upper()
        lines = [
            f"Campaign {report.run}",
            _report_display_status(report),
            f"{report.finding_count} findings",
            "",
            f"Saved {source_label} report",
            report.output,
            "",
            "Other formats",
        ]
        for format, label in (
            ("markdown", "Markdown"),
            ("json", "JSON"),
            ("csv", "CSV bundle (.zip)"),
        ):
            exported = next(
                (
                    item
                    for item in reversed(self.reports)
                    if item.run == report.run and item.format == format
                ),
                None,
            )
            sibling = (
                Path(report.output)
                .expanduser()
                .with_suffix({"markdown": ".md", "json": ".json", "csv": ".zip"}[format])
            )
            output = (
                exported.output
                if exported
                else str(sibling)
                if sibling.is_file()
                else "Use Export below to create"
            )
            lines.append(f"{label}: {output}")
        self.query_one("#overview-content", Static).update("\n".join(lines))

    @on(OptionList.OptionHighlighted, "#report-picker")
    def select_report(self, event: OptionList.OptionHighlighted) -> None:
        if self.busy:
            return
        self.selected_run = self.runs[event.option_index]
        self.query_one("#report-feedback", Static).update("")
        self._update_selected()

    async def action_open_html(self) -> None:
        report = self._source()
        if report is None or not report.output.endswith(".html"):
            return
        path = await asyncio.to_thread(lambda: Path(report.output).expanduser().resolve())
        if not await asyncio.to_thread(path.is_file):
            self.query_one("#report-feedback", Static).update(f"HTML file not found: {path}")
            return
        await asyncio.to_thread(webbrowser.open, path.as_uri())

    @on(OptionList.OptionSelected, "#report-picker")
    async def open_selected_report(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if not self.busy:
            self.selected_run = self.runs[event.option_index]
            self._update_selected()
            await self.action_open_html()

    def action_close(self) -> None:
        if not self.busy:
            self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        button_id = event.button.id or ""
        if button_id == "overview-close":
            self.action_close()
        elif button_id == "report-open-html":
            await self.action_open_html()
        elif button_id == "report-copy-path":
            report = self._source()
            if report is not None:
                self.app.copy_to_clipboard(report.output)
                self.query_one("#report-feedback", Static).update("HTML path copied")
        elif button_id.startswith("report-export-") and not self.busy:
            report = self._source()
            if report is None:
                return
            format = button_id.removeprefix("report-export-")
            self.busy = True
            self._update_selected()
            self.query_one("#report-feedback", Static).update(f"Exporting {format}…")
            try:
                exported = await self.backend.export_campaign_report(
                    report.run, report.output, format
                )
                self.reports.append(exported)
                self.query_one("#report-feedback", Static).update(f"Saved: {exported.output}")
            except Exception as exc:
                self.query_one("#report-feedback", Static).update(
                    "Could not export: " + _scrub_secrets(str(exc))
                )
            finally:
                self.busy = False
                self._update_selected()


class ExistingSessionScreen(ModalScreen[bool]):
    """Resolve a second launch without sending the user back to the shell."""

    DEFAULT_CSS = """
    ExistingSessionScreen { align: center middle; background: $background 70%; }
    #existing-session-dialog { width: 64; max-width: 90%; height: auto;
        border: round $accent; padding: 1 2; background: $surface; }
    #existing-session-dialog Label { text-style: bold; margin-bottom: 1; }
    #existing-session-dialog Button { width: 100%; margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "quit", "Quit", show=False)]

    def __init__(self, *, recover: bool = False) -> None:
        super().__init__()
        self.recover = recover

    def compose(self) -> ComposeResult:
        with Vertical(id="existing-session-dialog"):
            yield Label("Merlin needs to restart" if self.recover else "Merlin is already open")
            yield Static(
                "The previous session has stopped responding. Restart Merlin here?"
                if self.recover
                else "Start here instead? The existing session will close "
                "and its active work will stop.",
                markup=False,
            )
            yield Button(
                "Restart Merlin here" if self.recover else "Close existing session and start here",
                id="existing-session-replace",
            )
            yield Button("Quit", id="existing-session-quit")

    def on_mount(self) -> None:
        self.query_one("#existing-session-quit", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "existing-session-replace")

    def action_quit(self) -> None:
        self.dismiss(False)


class ToolConfirmationScreen(ModalScreen[bool]):
    """One-shot approval for an MCP tool whose effects are not known."""

    DEFAULT_CSS = """
    ToolConfirmationScreen { align: center bottom; background: transparent; padding: 0 1 5 1; }
    #dialog { width: 100%; max-width: 120; height: auto; padding: 1 2; background: #202020; }
    #dialog Button { width: 100%; height: 1; min-height: 1; margin: 0; padding: 0 1;
        border: none; background: #202020; text-align: left; }
    #dialog Button:focus { background: #153047; color: #82e9f5; text-style: bold; }
    """
    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("1", "allow", "Allow once", show=False),
        Binding("2", "deny", "Deny", show=False),
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(self, tool_name: str, arguments: Mapping[str, Any]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Allow MCP tool call?")
            yield Static(self.tool_name, id="tool-confirm-name", markup=False)
            yield Static(
                _render_tool_arguments(self.arguments),
                id="tool-confirm-arguments",
                markup=False,
            )
            yield Button("1. Allow once", id="tool-confirm-allow")
            yield Button("2. Deny", id="tool-confirm-deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "tool-confirm-allow")

    def action_deny(self) -> None:
        self.dismiss(False)

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, offset: int) -> None:
        buttons = list(self.query(Button))
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()


class LiveTranscript(RichLog):
    """RichLog with one replaceable tail row for an in-flight model stream."""

    _SELECTION_STYLE = Style(color="#ffffff", bgcolor="#264f78")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("max_lines", MAX_RENDERED_TRANSCRIPT_LINES)
        super().__init__(*args, **kwargs)
        self._live_start: int | None = None
        self._follow_tail = True
        self._selection_anchor: tuple[int, int] | None = None
        self._selection_focus: tuple[int, int] | None = None
        self._selecting = False
        self._line_text_bytes: list[int] = []
        self._rendered_text_bytes = 0

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        # scroll_y/scroll_x can be fractional during smooth scrolling, but line
        # selection indexes and strip cropping require integer offsets.
        scroll_y = int(self.scroll_y)
        scroll_x = int(self.scroll_x)
        selection = self._selection_for_line(scroll_y + y)
        if selection is None:
            return strip
        absolute_start, absolute_end = selection
        visible_start = max(0, absolute_start - scroll_x)
        visible_end = min(strip.cell_length, absolute_end - scroll_x)
        if visible_start >= visible_end:
            return strip
        return Strip.join(
            (
                strip.crop(0, visible_start),
                self._highlight(strip.crop(visible_start, visible_end)),
                strip.crop(visible_end),
            )
        )

    @staticmethod
    def _highlight(strip: Strip) -> Strip:
        """Apply the selection style on top of a strip's existing styles.

        ``Strip.apply_style`` composes ``style + segment.style``, so a segment's
        own background (the log's ``rich_style``) wins and the highlight stays
        invisible. Compose the selection as a post-style instead so it overrides.
        """
        from rich.segment import Segment

        return Strip(
            list(Segment.apply_style(strip, None, post_style=LiveTranscript._SELECTION_STYLE)),
            strip.cell_length,
        )

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            await super()._on_mouse_down(event)
            return
        self._follow_tail = False
        self._selection_anchor = self._mouse_location(event)
        self._selection_focus = self._selection_anchor
        self._selecting = True
        self.capture_mouse()
        self.refresh()
        event.stop()

    async def _on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._selecting:
            return
        self._selection_focus = self._mouse_location(event, captured=True)
        self.refresh()
        event.stop()

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._selecting:
            await super()._on_mouse_up(event)
            return
        self._selection_focus = self._mouse_location(event, captured=True)
        self._selecting = False
        self.release_mouse()
        self.refresh()
        if selected := self.selected_text:
            self.app.copy_to_clipboard(selected)
            self.app.notify("Selection copied to clipboard", severity="information")
        event.stop()

    def _mouse_location(
        self, event: events.MouseEvent, *, captured: bool = False
    ) -> tuple[int, int]:
        offset = (
            event.get_content_offset_capture(self) if captured else event.get_content_offset(self)
        )
        if offset is None:
            return (int(self.scroll_y), int(self.scroll_x))
        row = max(0, min(len(self.lines) - 1, int(self.scroll_y + offset.y)))
        column = max(0, int(self.scroll_x + offset.x))
        return (row, column)

    def _selection_for_line(self, row: int) -> tuple[int, int] | None:
        if self._selection_anchor is None or self._selection_focus is None:
            return None
        start, end = sorted((self._selection_anchor, self._selection_focus))
        if start == end or row < start[0] or row > end[0]:
            return None
        line_length = self.lines[row].cell_length
        start_column = start[1] if row == start[0] else 0
        end_column = end[1] if row == end[0] else line_length
        return (min(start_column, line_length), min(end_column, line_length))

    @property
    def selected_text(self) -> str:
        if self._selection_anchor is None or self._selection_focus is None or not self.lines:
            return ""
        start, end = sorted((self._selection_anchor, self._selection_focus))
        last_row = len(self.lines) - 1
        start = (min(start[0], last_row), start[1])
        end = (min(end[0], last_row), end[1])
        if start == end:
            return ""
        selected: list[str] = []
        for row in range(start[0], end[0] + 1):
            text = self.lines[row].text
            left = start[1] if row == start[0] else 0
            right = end[1] if row == end[0] else self.lines[row].cell_length
            left_index = cell_width_to_column_index(text, left, 4)
            right_index = cell_width_to_column_index(text, right, 4)
            selected.append(text[left_index:right_index])
        return "\n".join(selected)

    def write(
        self,
        content: Any,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> LiveTranscript:
        """Write while preserving operator-controlled sticky-tail behavior."""
        del scroll_end
        follow_tail = self._follow_tail and not self.is_vertical_scrollbar_grabbed
        if len(self._line_text_bytes) != len(self.lines):
            self._line_text_bytes = [len(line.text.encode("utf-8")) for line in self.lines]
            self._rendered_text_bytes = sum(self._line_text_bytes)
        previous_line_count = len(self.lines)
        previous_start_line = self._start_line
        super().write(
            content,
            width=width,
            expand=expand,
            shrink=shrink,
            scroll_end=False,
            animate=animate,
        )
        trimmed = self._start_line - previous_start_line
        new_line_count = max(0, len(self.lines) - previous_line_count + trimmed)
        if trimmed:
            removed = self._line_text_bytes[:trimmed]
            del self._line_text_bytes[:trimmed]
            self._rendered_text_bytes -= sum(removed)
        if new_line_count:
            new_sizes = [len(line.text.encode("utf-8")) for line in self.lines[-new_line_count:]]
            self._line_text_bytes.extend(new_sizes)
            self._rendered_text_bytes += sum(new_sizes)
        if trimmed > 0:
            if self._live_start is not None:
                self._live_start = max(0, self._live_start - trimmed)
            self._selection_anchor = None
            self._selection_focus = None
        self._trim_rendered_bytes()
        if follow_tail:
            self.call_after_refresh(self._scroll_to_tail)
        return self

    def _trim_rendered_bytes(self) -> None:
        trimmed = 0
        retained_bytes = self._rendered_text_bytes
        for line_bytes in self._line_text_bytes[:-1]:
            if retained_bytes <= MAX_RENDERED_TRANSCRIPT_BYTES:
                break
            retained_bytes -= line_bytes
            trimmed += 1
        if not trimmed:
            return
        del self._line_text_bytes[:trimmed]
        del self.lines[:trimmed]
        self._rendered_text_bytes = retained_bytes
        self._start_line += trimmed
        if self._live_start is not None:
            self._live_start = max(0, self._live_start - trimmed)
        self._selection_anchor = None
        self._selection_focus = None
        self._line_cache.clear()
        self._widest_line_width = max((line.cell_length for line in self.lines), default=0)
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh()

    def _scroll_to_tail(self) -> None:
        if self._follow_tail and not self.is_vertical_scrollbar_grabbed:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._follow_tail = False
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def scroll_page_up(self, **kwargs: Any) -> None:
        self._follow_tail = False
        super().scroll_page_up(**kwargs)

    def scroll_page_down(self, **kwargs: Any) -> None:
        super().scroll_page_down(**kwargs)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def action_scroll_end(self) -> None:
        self._follow_tail = True
        super().action_scroll_end()

    def _resume_follow_if_at_end(self) -> None:
        if self.is_vertical_scroll_end:
            self._follow_tail = True

    def replace_live(self, content: Text) -> None:
        if self._live_start is None:
            self._live_start = len(self.lines)
        self._truncate_live()
        self.write(content, expand=True, shrink=True)

    def commit_live(self) -> None:
        self._live_start = None

    def discard_live(self) -> None:
        if self._live_start is None:
            return
        self._truncate_live()
        self._live_start = None

    def _truncate_live(self) -> None:
        assert self._live_start is not None
        removed_sizes = self._line_text_bytes[self._live_start :]
        del self._line_text_bytes[self._live_start :]
        self._rendered_text_bytes -= sum(removed_sizes)
        del self.lines[self._live_start :]
        self._line_cache.clear()
        self._widest_line_width = max(
            (line.cell_length for line in self.lines),
            default=0,
        )
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh()


class TranscriptCopyScreen(ModalScreen[None]):
    """Selectable plain-text transcript view for terminal-safe copying."""

    BINDINGS = [Binding("escape", "close", "Close", show=False)]
    DEFAULT_CSS = """
    TranscriptCopyScreen { align: center middle; background: $background 70%; }
    #transcript-copy-dialog {
        width: 92%; height: 88%; padding: 1 2; background: #171717;
        border: round #3a3a3a;
    }
    #transcript-copy-title { height: 1; text-style: bold; color: #d7d7d7; }
    #transcript-copy-help { height: 1; color: #888888; margin-bottom: 1; }
    #transcript-copy-text { height: 1fr; border: none; background: #111111; }
    """

    def __init__(self, transcript: str) -> None:
        super().__init__()
        self.transcript = transcript

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-copy-dialog"):
            yield Static(
                f"Copy recent transcript (up to {MAX_RENDERED_TRANSCRIPT_LINES:,} rendered lines)",
                id="transcript-copy-title",
            )
            yield Static(
                "Drag to highlight, then Ctrl+C · Esc closes",
                id="transcript-copy-help",
            )
            yield TextArea(self.transcript, read_only=True, id="transcript-copy-text")

    def on_mount(self) -> None:
        self.query_one("#transcript-copy-text", TextArea).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class MerlinApp(App[None]):
    TITLE = "Merlin"
    SUB_TITLE = "AI security harness"
    CSS = """
    Screen { layout: vertical; background: #0d0d0d; }
    #conversation { width: 100%; height: 1fr; padding: 1 3 0 3; align: center middle; }
    #welcome { width: 100%; height: auto; content-align: center middle; color: cyan; }
    #welcome.hidden { display: none; }
    #transcript {
        display: none; height: 1fr; width: 100%; border: none;
        padding: 0; background: #0d0d0d; scrollbar-size: 1 1;
    }
    #transcript.visible { display: block; }
    #activity { display: none; height: 1; margin: 0 5; color: $text-muted; }
    #activity.visible { display: block; }
    #swarm-live {
        display: none; height: auto; max-height: 16; margin: 0 3;
        padding: 0 1; border-top: solid #315b73; border-bottom: solid #315b73;
        background: #111820; scrollbar-size: 1 1;
    }
    #swarm-live.visible { display: block; }
    #scan-actions { display: none; height: 3; margin: 0 3; }
    #scan-actions.visible { display: block; }
    #scan-next { display: none; height: auto; margin: 0 3; color: $text-muted; }
    #scan-next.visible { display: block; }
    #scan-actions Button { min-width: 10; width: auto; margin-right: 1; }
    #command-hints {
        display: none; height: auto; max-height: 8; margin: 0 3;
        padding: 0 1; border-left: thick #5f87af; color: $text-muted;
        background: #151515;
    }
    #command-hints.visible { display: block; }
    #permission-drawer {
        display: none; width: 1fr; height: auto; max-height: 80%;
        margin: 0 3; padding: 1 2; background: #202020;
    }
    #permission-drawer.visible { display: block; }
    #permission-title { height: 1; text-style: bold; color: #d7d7d7; }
    #permission-scope { height: auto; color: #a8a8a8; margin-bottom: 1; }
    #permission-details-scroll {
        height: auto; max-height: 6; scrollbar-size: 1 1; margin-bottom: 1;
    }
    #permission-details { height: auto; }
    #permission-help { height: 1; color: $text-muted; }
    #permission-allow-command-session { display: none; }
    #permission-drawer Button {
        width: 100%; height: 1; min-height: 1; margin: 0; padding: 0 1;
        border: none; background: #202020; color: #bcbcbc; text-align: left;
    }
    #permission-drawer Button:focus {
        background: #153047; color: #82e9f5; text-style: bold;
    }
    #composer {
        width: 1fr; height: 3; min-height: 3; max-height: 5;
        margin: 0 3; padding: 1 1;
        border: none; background: #191919;
    }
    #composer:focus { background: #202020; }
    #composer-help { height: 1; margin: 0 5; color: #777777; }
    #status {
        height: auto; min-height: 1; max-height: 3; padding: 0 5;
        background: #0d0d0d; color: $text-muted;
    }
    """
    BINDINGS = [
        Binding("ctrl+p", "palette", "Commands", priority=True),
        Binding("ctrl+o", "details", "Expand", priority=True),
        Binding("ctrl+shift+c", "copy_transcript", "Copy transcript", priority=True),
        Binding("ctrl+c", "interrupt", "Interrupt", show=False),
        Binding("ctrl+z", "quit_app", "Exit", show=False, priority=True),
        Binding("ctrl+d", "quit_app", "Exit", show=False, priority=True),
        Binding("escape", "interrupt", "Interrupt", show=False),
        Binding("ctrl+up", "scroll_up", "Scroll transcript up", show=False),
        Binding("ctrl+down", "scroll_down", "Scroll transcript down", show=False),
    ]

    def __init__(
        self,
        backend: ChatBackend,
        *,
        client_state: ClientState | None = None,
        startup_prompt: bool = False,
    ) -> None:
        super().__init__()
        self.backend = backend
        self._client_state = client_state or ClientState()
        self._transcript_entries: list[TranscriptEntry] = []
        self._stream_task: asyncio.Task[None] | None = None
        self._permission_mode = "auto"
        self._activity_history: list[str] = []
        self._active_thinking = ""
        self._startup_prompt = startup_prompt
        self._work_started_at: float | None = None
        self._work_label = "Working"
        self._usage_summary = ""
        self._spinner_index = 0
        self._permission_result: asyncio.Future[str] | None = None
        self._permission_lock = asyncio.Lock()
        self._permission_previous_focus: Any | None = None
        self._assistant_pending = False
        self._provider_name = "provider"
        self._model_name = "loading"
        self._effort = "default"
        self._pending_choice: tuple[tuple[str, str], ...] = ()
        self._pending_choice_default = ""
        self._pending_choice_kind = ""
        self._pending_profile_answer = ""
        self._pending_campaign_review: tuple[str, str] | None = None
        self._active_saturation_request: str | None = None
        self._known_report_revisions: set[tuple[str, str]] | None = None
        self._pending_report_notifications: dict[tuple[str, str], ReportView] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._swarm_refresh_lock = asyncio.Lock()
        self._swarm_rendered = ""
        self._scan_summary_report: ReportView | None = None
        self._unmounting = False
        self._active_session_id = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="conversation"):
            yield Static(
                Text(
                    "\n".join(
                        (
                            *WORDMARK,
                            "security evaluation harness for AI systems",
                            *WELCOME_GUIDANCE,
                        )
                    ),
                    justify="center",
                ),
                id="welcome",
            )
            yield LiveTranscript(id="transcript", wrap=True, markup=True)
        yield Static("", id="activity", markup=False)
        yield LiveSwarmLog(
            id="swarm-live",
            wrap=True,
            markup=False,
            max_lines=32,
            min_width=1,
            auto_scroll=False,
        )
        with Horizontal(id="scan-actions"):
            yield Button("Open HTML", id="scan-open-html")
            yield Button("Reports", id="scan-reports")
            yield Button("Markdown", id="scan-export-markdown", classes="scan-export")
            yield Button("JSON", id="scan-export-json", classes="scan-export")
            yield Button("CSV", id="scan-export-csv", classes="scan-export")
            yield Button("Recover", id="scan-recover", disabled=True)
        yield Static("Enter /new to start a new session.", id="scan-next", markup=False)
        yield Static("", id="command-hints", markup=False)
        with Vertical(id="permission-drawer"):
            yield Static("Permission required", id="permission-title", markup=False)
            with VerticalScroll(id="permission-details-scroll", can_focus=False):
                yield Static("", id="permission-details", markup=False)
                yield Static("", id="permission-scope", markup=False)
            yield Static(
                "Scroll / Page Up / Page Down: inspect details", id="permission-help", markup=False
            )
            yield Button("1. Allow once", id="permission-allow-once")
            yield Button(
                "2. Allow this command + directory (session)", id="permission-allow-command-session"
            )
            yield Button("2. Allow this scope for this session", id="permission-allow-session")
            yield Button("3. Deny", id="permission-deny-once")
            yield Button("4. Deny this scope for this session", id="permission-deny-session")
        yield Composer(name="Message composer", id="composer")
        yield Static(
            "Enter send · Shift+Enter newline · / commands · Ctrl+O details · "
            "Ctrl+Shift+C copy · Ctrl+Z exit",
            id="composer-help",
            markup=False,
        )
        yield Static("ready · /help for commands", id="status")

    def on_mount(self) -> None:
        composer = self.query_one("#composer", Composer)
        composer.focus()
        self.set_interval(0.12, self._refresh_work_indicator)
        self._spawn(self._initialize_foreground())

    async def _initialize_foreground(self) -> None:
        if not await self._acquire_foreground_lease():
            return
        self._set_status("ready")
        self.set_interval(2.0, self._poll_saturation_decisions)
        self.set_interval(2.0, self._poll_completed_reports)
        self.set_interval(2.0, self._schedule_swarm_refresh)
        self._spawn(self._load_permission_mode())
        self._spawn(self._load_runtime_status())
        if self._startup_prompt:
            self._spawn(self._show_startup_session_gate())
        else:
            self._spawn(self._sync_active_session())

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Own every app background task so teardown can cancel and await it."""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                self.log.error("background task failed", error)

        task.add_done_callback(finished)
        if self._unmounting:
            task.cancel()
        return task

    def _schedule_swarm_refresh(self, _result: None = None) -> None:
        """Keep polling alive independently of modal-screen callback teardown."""
        if self._unmounting:
            return
        pending = getattr(self, "_swarm_poll_task", None)
        if pending is None or pending.done():
            self._swarm_poll_task = self._spawn(self._poll_swarm_progress())

    async def _acquire_foreground_lease(self) -> bool:
        acquire = getattr(self.backend, "acquire_foreground_lease", None)
        if not callable(acquire):
            return True
        try:
            watch = getattr(self.backend, "wait_for_foreground_close", None)
            if callable(watch):
                await acquire(close_notifications=True)
                self._spawn(self._watch_foreground_close())
            else:
                await acquire()
            return True
        except OSError as exc:
            if callable(getattr(self.backend, "replace_foreground", None)):
                return await self._resolve_existing_session(recover=True)
            self.notify(f"Local daemon is unavailable: {exc}", severity="error")
            self.exit(
                return_code=1,
                message=Text(f"Merlin could not connect to its local daemon: {exc}"),
            )
            return False
        except Exception as exc:
            if "already has a foreground owner" in str(exc):
                replace = getattr(self.backend, "replace_foreground", None)
                if callable(replace):
                    return await self._resolve_existing_session()
            self.notify(f"Foreground ownership was refused: {exc}", severity="error")
            self.exit(
                return_code=1,
                message=Text(
                    f"Merlin could not open this window: {exc}. "
                    "Close the existing Merlin window with /quit, then run ./merlin again."
                ),
            )
            return False

    async def _watch_foreground_close(self) -> None:
        try:
            await self.backend.wait_for_foreground_close()
        except (ConnectionError, OSError, RuntimeError) as exc:
            if not self._unmounting:
                self.exit(return_code=1, message=Text(str(exc)))
        else:
            self.exit(message=Text("This Merlin session was closed from another window."))

    async def _resolve_existing_session(self, *, recover: bool = False) -> bool:
        while True:
            choice: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

            def resolved(replace: bool, result: asyncio.Future[bool] = choice) -> None:
                if not result.done():
                    result.set_result(replace)

            self.push_screen(ExistingSessionScreen(recover=recover), resolved)
            if not await choice:
                self.exit()
                return False
            self._set_status("restarting Merlin · preserving saved sessions…")
            try:
                await self.backend.replace_foreground()
                await self.backend.acquire_foreground_lease(close_notifications=True)
            except Exception as exc:
                self.notify(f"Could not start here: {exc}", severity="error", timeout=15)
            else:
                self._spawn(self._watch_foreground_close())
                return True

    async def _show_startup_session_gate(self) -> None:
        screen = StartupSessionScreen(())
        self.query_one("#composer", Composer).disabled = True
        await self.push_screen(
            screen, lambda choice: self._finish_startup_choice(choice, screen.sessions)
        )
        await self._load_startup_sessions(screen)

    def on_startup_session_screen_retry_requested(
        self, event: StartupSessionScreen.RetryRequested
    ) -> None:
        screen = self.screen
        if (
            isinstance(screen, StartupSessionScreen)
            and not screen.query_one("#startup-browse", Button).disabled
        ):
            # Disable synchronously so repeated keys cannot launch parallel lookups.
            screen.query_one("#startup-browse", Button).disabled = True
            self._spawn(self._load_startup_sessions(screen))

    async def _load_startup_sessions(self, screen: StartupSessionScreen) -> None:
        screen.query_one("#startup-title", Static).update("Loading previous sessions…")
        screen.query_one("#startup-detail", Static).update(
            "You can start a new session while history loads."
        )
        screen.query_one("#startup-help", Static).update("Enter starts a new session")
        sessions: tuple[ResumeSessionView, ...] = ()
        error: Exception | None = None
        for attempt in range(2):
            try:
                # The screen is already interactive. Slow history must not choose
                # New session for the operator or cancel a legitimate daemon lookup.
                sessions = await self.backend.list_resume_sessions()
                error = None
                break
            except Exception as exc:
                error = exc
                if attempt == 0:
                    await asyncio.sleep(0.1)
        if not screen.is_mounted or screen not in self.screen_stack:
            return
        if error is not None:
            screen.show_history_error()
            return
        if not sessions:
            # An empty lookup is not an operator decision. In particular, a
            # startup race must not silently discard the resume chooser.
            screen.query_one("#startup-title", Static).update("No saved sessions found")
            screen.query_one("#startup-detail", Static).update(
                "Start a new session, or retry if you expected saved history."
            )
            retry = screen.query_one("#startup-browse", Button)
            retry.label = "3. Retry loading previous sessions"
            retry.disabled = False
            screen.query_one("#startup-help", Static).update(
                "R or 3 retries · Enter starts a new session"
            )
            return
        screen.show_sessions(sessions)

    def _finish_startup_choice(
        self, choice: str | None, sessions: tuple[ResumeSessionView, ...]
    ) -> None:
        if choice == "browse":
            self._spawn(self._open_startup_session_page(sessions=sessions))
            return
        self._spawn(self._apply_startup_choice(choice, sessions))

    async def _open_startup_session_page(
        self,
        *,
        sessions: tuple[ResumeSessionView, ...],
        offset: int = 0,
        query: str = "",
    ) -> None:
        loader = getattr(self.backend, "list_resume_session_page", None)
        if callable(loader):
            page = await loader(limit=20, offset=offset, query=query)
        else:
            matches = tuple(
                item
                for item in sessions
                if query.casefold() in f"{item.id} {item.title}".casefold()
            )
            page = ResumeSessionPage(matches[offset : offset + 20], len(matches), 20, offset, query)
        self.push_screen(
            SessionBrowserScreen(page),
            lambda action: self._finish_startup_browse(action, page, sessions),
        )

    def _finish_startup_browse(
        self,
        action: str | None,
        page: ResumeSessionPage,
        sessions: tuple[ResumeSessionView, ...],
    ) -> None:
        if action == "next":
            self._spawn(
                self._open_startup_session_page(
                    sessions=sessions, offset=page.offset + page.limit, query=page.query
                )
            )
        elif action == "previous":
            self._spawn(
                self._open_startup_session_page(
                    sessions=sessions,
                    offset=max(0, page.offset - page.limit),
                    query=page.query,
                )
            )
        elif isinstance(action, str) and action.startswith("search:"):
            self._spawn(
                self._open_startup_session_page(
                    sessions=sessions, query=action.removeprefix("search:").strip()
                )
            )
        else:
            self._spawn(self._apply_startup_choice(action or "new", sessions))

    async def _apply_startup_choice(
        self, choice: str | None, sessions: tuple[ResumeSessionView, ...]
    ) -> None:
        composer = self.query_one("#composer", Composer)
        composer.disabled = False
        composer.focus()
        try:
            resume_id = (
                choice.removeprefix("resume:")
                if isinstance(choice, str) and choice.startswith("resume:")
                else sessions[0].id
                if choice == "resume" and sessions
                else None
            )
            if resume_id is not None:
                self._active_session_id = resume_id
                self._activate_conversation()
                await self._start_backend(f"/resume {resume_id}", show_assistant=False)
                if self._stream_task is not None:
                    await self._stream_task
            else:
                self._active_session_id = ""
                async with asyncio.timeout(5.0):
                    async for _event in self.backend.stream("/new"):
                        pass
                await self._sync_active_session()
        except Exception as exc:
            detail = (
                "The daemon did not finish creating the session within 5 seconds."
                if isinstance(exc, TimeoutError) and resume_id is None
                else str(exc).strip() or type(exc).__name__
            )
            message = (
                f"Could not initialize the session: {detail}\n"
                "Open /resume to select a saved session, or use /new to retry a new session. "
                "Saved campaign checkpoints have not been deleted."
            )
            self._activate_conversation()
            self.query_one("#transcript", RichLog).write(Text(message, style="yellow"))
            self.notify(message, severity="error", timeout=15)
        finally:
            composer.focus()
            self._spawn(self._load_permission_mode())
            self._spawn(self._load_runtime_status())

    def _activate_conversation(self) -> None:
        self.query_one("#welcome", Static).add_class("hidden")
        self.query_one("#transcript", RichLog).add_class("visible")

    def action_quit_app(self) -> None:
        self.exit()

    @on(Composer.Submitted)
    async def submit_composer(self) -> None:
        await self.action_send()

    @on(TextArea.Changed, "#composer")
    def update_command_hints(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, Composer):
            event.text_area.resize_for_content()
        text = event.text_area.text.lstrip()
        try:
            hints = self.query_one("#command-hints", Static)
        except NoMatches:
            # TextArea emits one final Changed event while the app is unmounting.
            return
        if not text.startswith("/"):
            hints.remove_class("visible")
            hints.update("")
            return
        matches = [command for command in COMMANDS if command.startswith(text)]
        if not matches:
            suggestion = _suggest_command(text)
            hints.update(f"Did you mean {suggestion}?" if suggestion else "No matching command")
        else:
            hints.update(
                "\n".join(f"{command:<24} {COMMAND_HINTS[command]}" for command in matches[:8])
            )
        hints.add_class("visible")

    async def action_send(self) -> None:
        composer = self.query_one("#composer", Composer)
        text = composer.text.strip()
        composer.remember_submission(text)
        if text == "/web":
            composer.clear()
            await self._run_command(text)
            return
        startup = getattr(self, "_campaign_start_task", None)
        if startup is not None and not startup.done() and not text.startswith("/"):
            self.notify("Campaign startup is already in progress", severity="information")
            return
        if (
            self._pending_choice_kind == "campaign_review"
            and self._pending_campaign_review is not None
            and text.lower() in {"", "1", "approve"}
        ):
            # This is an operator decision, not model steering. Execution does
            # not replace the foreground reader, so it need not wait for the
            # model's post-preparation response to finish streaming.
            review = self._pending_campaign_review
            self._clear_pending_decision()
            composer.clear()
            self._campaign_start_task = self._spawn(self._execute_approved_campaign(*review))
            return
        if text and self._pending_choice:
            answers = {value.lower() for _, value in self._pending_choice}
            answers.update(str(i) for i in range(1, len(self._pending_choice) + 1))
            if text.lower() not in answers:
                # Numbered decisions are shortcuts, not a barrier to chat. A new
                # instruction invalidates the old approval rather than leaving
                # it armed while another campaign is being prepared.
                self._clear_pending_decision()
        # Interruption must remain available while a numbered decision is pending.
        # The foreground daemon may still be doing reconnaissance at that point.
        if text == "/interrupt":
            composer.clear()
            await self.action_interrupt()
            return
        console = getattr(self, "_web_console", None)
        web_turn = console.workspace.turn if console else None
        stream_task = self._stream_task or web_turn
        if text and not text.startswith("/") and stream_task and not stream_task.done():
            steer = getattr(self.backend, "steer", None)
            if callable(steer):
                try:
                    accepted = await steer(text)
                except Exception as exc:
                    self.query_one("#transcript", RichLog).write(
                        _transcript_text("✕ Message failed: ", exc, "bold red")
                    )
                    return
                if accepted:
                    # The existing event reader renders the message after flushing
                    # its live response tail. Never replace it with a second stream.
                    composer.clear()
                    self._save_client_state("")
                    return
            # Drain the old reader before its finally block can clear a new one.
            await stream_task
        if self._pending_choice:
            if not text:
                text = self._pending_choice_default
            else:
                numeric = {
                    str(index): value
                    for index, (_label, value) in enumerate(self._pending_choice, start=1)
                }
                text = numeric.get(text, text)
            valid = {value for _label, value in self._pending_choice}
            if text.lower() not in valid:
                choices = " or ".join(
                    f"{index} ({label})"
                    for index, (label, _value) in enumerate(self._pending_choice, start=1)
                )
                self.query_one("#transcript", RichLog).write(
                    Text(
                        f"Choose {choices}, or press Enter for the default.",
                        style="yellow",
                    )
                )
                return
            kind = self._pending_choice_kind
            self._pending_choice = ()
            self._pending_choice_default = ""
            self._pending_choice_kind = ""
            if kind == "profile":
                self._pending_profile_answer = text
                composer.clear()
                self._pending_choice = (
                    ("Autonomous [default]", "autonomous"),
                    ("Opinionated", "opinionated"),
                )
                self._pending_choice_default = "autonomous"
                self._pending_choice_kind = "composition"
                self.query_one("#transcript", RichLog).write(
                    "\n[bold]Agent composition[/bold] "
                    "(independent of the coverage profile)\n"
                    "[bold cyan]1.[/] [bold]Autonomous[/bold] "
                    "[bold yellow]\\[default][/]\n"
                    "[dim]   Campaign Lead composes a target-specific agent team · adds "
                    "planning time, may improve fit[/]\n"
                    "[bold green]2.[/] Opinionated\n"
                    "[dim]   Uses Merlin's reviewed agent template · faster, "
                    "predictable startup[/]\n"
                    "[dim]────────────────────────────────────────────────[/]\n"
                    "[dim]Press Enter or 1 for Autonomous · 2 for Opinionated[/]"
                )
                return
            if kind == "composition":
                profile_answer = self._pending_profile_answer
                self._pending_profile_answer = ""
                text = (
                    f"{profile_answer}\nThe operator explicitly selected specialist "
                    f"composition: {text} (orthogonal to the coverage profile); pass "
                    f"specialist_composition={text!r} to assessment_prepare and do not "
                    "ask again."
                )
            if kind == "campaign_review":
                review = self._pending_campaign_review
                self._pending_campaign_review = None
                if text == "approve" and review is not None:
                    # Approval is the operator's decision; execution itself must
                    # not depend on the model choosing to call assessment_execute.
                    composer.clear()
                    await self._execute_approved_campaign(*review)
                    return
        if not text:
            return
        self._activate_conversation()
        composer.clear()
        self._save_client_state("")
        if text.startswith("/"):
            suggestion = _suggest_command(text)
            root = text.split(maxsplit=1)[0].lower()
            if suggestion is not None or root not in _ROOT_COMMANDS:
                correction = (
                    f" Did you mean [bold]{suggestion}[/]?"
                    if suggestion
                    else " Try [bold]/help[/]."
                )
                self.query_one("#transcript", RichLog).write(
                    f"[bold red]✕ Unknown command.[/]{correction}"
                )
                return
            await self._run_command(text)
            return
        transcript = self.query_one("#transcript", LiveTranscript)
        user_row = Text("\n")
        user_row.append("● ", style="bold cyan")
        user_row.append(text)
        transcript.write(user_row)
        self._remember("user", text)
        if console:
            console.workspace.emit("user", text)
        # Activating the transcript changes layout from ``display: none``. Start the provider
        # only after Textual has rendered the operator row, so the replaceable reasoning tail
        # is anchored immediately after it rather than racing deferred RichLog writes.
        self.call_after_refresh(self._start_backend, text, show_assistant=True)

    async def _execute_approved_campaign(self, campaign_id: str, manifest_digest: str) -> None:
        """Start an operator-approved campaign directly against the daemon."""
        transcript = self.query_one("#transcript", LiveTranscript)
        user_row = Text("\n")
        user_row.append("● ", style="bold cyan")
        user_row.append("approve")
        transcript.write(user_row)
        self._remember("user", "approve")
        self._activate_conversation()
        started_at = time.monotonic()
        startup = Static("", id="campaign-start", markup=False)
        startup.styles.height = "auto"
        startup.styles.padding = (0, 1)
        startup.styles.border = ("round", "cyan")
        frame = 0

        def render_startup() -> None:
            nonlocal frame
            elapsed = int(time.monotonic() - started_at)
            spinner = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")[frame % 10]
            frame += 1
            message = Text(f"{spinner} Starting campaign · {elapsed}s", style="bold cyan")
            message.append("\nApproval received · waiting for the daemon to confirm startup.")
            if elapsed >= 30:
                message.append(
                    "\nStartup is taking longer than expected. Inspect /campaign status "
                    + campaign_id,
                    style="yellow",
                )
            startup.update(message)

        render_startup()
        await self.mount(startup, before=self.query_one("#activity"))
        timer = self.set_interval(0.12, render_startup)
        transcript.write(Text("Approval received · starting campaign…", style="cyan"))
        try:
            await self.backend.execute_campaign(campaign_id, manifest_digest)
        except Exception as exc:
            transcript.write(_transcript_text("✕ Campaign execution failed: ", exc, "bold red"))
            self._remember("error", _scrub_secrets(f"Campaign execution failed: {exc}"))
            return
        finally:
            timer.stop()
            await startup.remove()
        console = getattr(self, "_web_console", None)
        if console:
            console.workspace.latest_review = None
        transcript.write(
            Text(
                f"\nCampaign started: {campaign_id}\nMonitor: /campaign status {campaign_id}",
                style="green",
            )
        )
        self._remember("status", f"Campaign {campaign_id} executing")
        self._operator_preparing_session = ""
        self._schedule_swarm_refresh()

    def _clear_pending_decision(self) -> None:
        self._pending_choice = ()
        self._pending_choice_default = ""
        self._pending_choice_kind = ""
        self._pending_profile_answer = ""
        self._pending_campaign_review = None

    async def _stream(self, prompt: str) -> None:
        transcript = self.query_one("#transcript", LiveTranscript)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        turn_content: list[str] = []
        last_render_at = 0.0
        pending_render: Callable[[], None] | None = None
        render_timer: Any | None = None
        operator_commands: list[tuple[str, str]] = []
        input_request = None
        input_context = None
        campaign_review_text: str | None = None

        def cancel_render() -> None:
            nonlocal pending_render, render_timer
            pending_render = None
            if render_timer is not None:
                render_timer.stop()
                render_timer = None

        def render_pending() -> None:
            nonlocal pending_render, render_timer, last_render_at
            callback = pending_render
            pending_render = None
            render_timer = None
            if callback is not None:
                callback()
                last_render_at = time.monotonic()

        def flush_thinking() -> None:
            if not thinking_parts:
                return
            thinking = "".join(thinking_parts)
            cancel_render()
            show_thinking()
            transcript.commit_live()
            self._record_activity(f"• Thinking · {thinking}")
            self._remember("thinking", thinking)
            thinking_parts.clear()
            self._active_thinking = ""
            self._set_thinking("")

        def flush_content() -> None:
            if not content_parts:
                return
            content = "".join(content_parts)
            cancel_render()
            turn_content.append(content)
            show_content()
            transcript.commit_live()
            self._assistant_pending = False
            self._remember("content", content)
            content_parts.clear()

        def show_content() -> None:
            rendered = Text()
            if self._assistant_pending:
                rendered.append("● ", style="bold green")
            rendered.append_text(_render_structured_choice_content("".join(content_parts)))
            transcript.replace_live(rendered)

        def show_thinking() -> None:
            self._active_thinking = "".join(thinking_parts)
            self._set_thinking(self._active_thinking)

        async def reveal(delta: str, parts: list[str], render: Callable[[], None]) -> None:
            nonlocal pending_render, render_timer
            parts.append(delta)
            pending_render = render
            remaining = 1 / 30 - (time.monotonic() - last_render_at)
            if remaining <= 0:
                if render_timer is not None:
                    render_timer.stop()
                render_pending()
            elif render_timer is None:
                render_timer = self.set_timer(remaining, render_pending)
            await asyncio.sleep(0)

        try:
            async for event in self.backend.stream(prompt):
                if event.kind == "campaign_review":
                    self._web_review = event.text
                    campaign_review_text = event.text
                console = getattr(self, "_web_console", None)
                if console:
                    if event.kind == "status" and event.text in {"new session", "session resumed"}:
                        console.workspace.emit("session_reset", None)
                    console.workspace.emit(event.kind, event.text)
                if event.kind == "content":
                    flush_thinking()
                    self._work_label = "Responding"
                    await reveal(event.text, content_parts, show_content)
                    continue
                # Providers may interleave reasoning and usage updates between individual
                # content tokens. Those are metadata for the active response, not transcript
                # boundaries; flushing here turns every token into a separate RichLog row.
                if event.kind == "usage":
                    self._usage_summary = event.text
                    continue
                if event.kind == "thinking":
                    flush_content()
                    self._work_label = "Thinking"
                    await reveal(
                        event.text,
                        thinking_parts,
                        show_thinking,
                    )
                    continue
                flush_thinking()
                flush_content()
                if event.kind == "user":
                    self._clear_pending_decision()
                    transcript.write(_transcript_text("\n● ", event.text, "bold cyan"))
                    self._remember("user", event.text)
                elif event.kind == "tool":
                    self._record_activity(f"↳ Tool · {event.text}")
                    transcript.write(_transcript_text("↳ Tool · ", event.text, "dim"))
                elif event.kind == "user_input":
                    from merlin_contracts.user_input import InputRequest

                    self._clear_pending_decision()
                    input_request = InputRequest.model_validate_json(event.text)
                    if campaign_review_text is not None:
                        input_context = _render_campaign_review(campaign_review_text)
                    self._remember("user_input", event.text)
                    transcript.write(
                        Text(
                            "Waiting for your answer: "
                            + " · ".join(question.question for question in input_request.questions),
                            style="cyan",
                        )
                    )
                elif event.kind == "user_input_resolved":
                    from .user_input_screen import UserInputScreen

                    if (
                        isinstance(self.screen, UserInputScreen)
                        and self.screen.request.request_id == event.text
                    ):
                        self.screen.dismiss(None)
                    self._remember("user_input_resolved", event.text)
                elif event.kind == "campaign_configure":
                    self._clear_pending_decision()
                    self._operator_preparing_session = self._active_session_id
                    self._schedule_swarm_refresh()
                elif event.kind == "operator_command":
                    try:
                        request = json.loads(event.text)
                        command = request["command"]
                        request_id = request["request_id"]
                        if (
                            request.get("status") == "queued"
                            and isinstance(command, str)
                            and isinstance(request_id, str)
                            and request_id
                            and command.split()[0] in _ROOT_COMMANDS
                            and "\n" not in command
                            and "\r" not in command
                        ):
                            operator_commands.append((request_id, command))
                    except (ValueError, TypeError, KeyError, IndexError):
                        transcript.write("[red]Invalid chat control request[/]")
                elif event.kind == "campaign_review":
                    transcript.write(_render_campaign_review(event.text))
                    self._remember("status", "Campaign manifest prepared for operator review")
                    try:
                        review_payload = json.loads(event.text)
                    except (TypeError, ValueError):
                        review_payload = {}
                    review_detail = (
                        review_payload.get("review", {})
                        if isinstance(review_payload, Mapping)
                        else {}
                    )
                    target_contract = (
                        review_detail.get("target_contract", {})
                        if isinstance(review_detail, Mapping)
                        else {}
                    )
                    current_interface = str(target_contract.get("interface", ""))
                    current_mode = str(
                        target_contract.get(
                            "execution_mode",
                            {
                                "http_api": "api",
                                "browser": "browser",
                            }.get(current_interface, current_interface),
                        )
                    )
                    options = target_contract.get("execution_options", ())
                    mode_choices = tuple(
                        (
                            f"Use {str(mode).upper()} and review again",
                            f"call campaign_configure with execution_mode={mode!r}, then call "
                            "assessment_prepare now using the same objective, profile, and "
                            f"recon_handoff_id, with adapter.execution_mode={mode!r}. do not "
                            "submit a revision note; return the revised campaign review.",
                        )
                        for mode in options
                        if isinstance(mode, str) and mode != current_mode
                    )
                    self._pending_choice = (
                        ("Approve [default]", "approve"),
                        *mode_choices,
                        ("Request other changes", "revise"),
                        ("Cancel campaign preparation", "cancel"),
                    )
                    self._pending_choice_default = "approve"
                    self._pending_choice_kind = "campaign_review"
                    review_campaign_id = (
                        str(review_payload.get("campaign_id") or "")
                        if isinstance(review_payload, Mapping)
                        else ""
                    )
                    review_digest = (
                        str(review_payload.get("manifest_digest") or "")
                        if isinstance(review_payload, Mapping)
                        else ""
                    )
                    self._pending_campaign_review = (
                        (review_campaign_id, review_digest)
                        if review_campaign_id and review_digest
                        else None
                    )
                    transcript.write(_render_operator_choices(self._pending_choice))
                elif event.kind == "skill":
                    self._record_activity(f"↳ Skill · {event.text}")
                    transcript.write(_transcript_text("↳ Skill · ", event.text, "cyan"))
                elif event.kind == "subagent":
                    self._record_activity(f"↳ Subagent · {event.text}")
                    transcript.write(_transcript_text("↳ Subagent · ", event.text, "magenta"))
                elif event.kind == "swarm":
                    self._record_activity(f"● Swarm · {event.text}")
                    transcript.write(_transcript_text("● Swarm · ", event.text, "blue"))
                elif event.kind == "status":
                    if prompt.strip() == "/new" and event.text == "new session":
                        await self._sync_active_session()
                        self._reset_new_session_view()
                        turn_content.clear()
                        continue
                    self._record_activity(event.text)
                    transcript.write(_transcript_text("", event.text, "dim"))
                    self._remember("status", event.text)
                elif event.kind == "attacker":
                    transcript.write(_transcript_text("\nAttacker › ", event.text, "bold magenta"))
                elif event.kind == "target":
                    transcript.write(_transcript_text("\nTarget ‹ ", event.text, "bold blue"))
                elif event.kind == "quit":
                    self.exit()
                else:
                    transcript.write(_transcript_text("✕ ", event.text, "bold red"))
            flush_thinking()
            flush_content()
            if input_request is None:
                self._offer_structured_choice("".join(turn_content))
        except asyncio.CancelledError:
            operator_commands.clear()
            if not self._unmounting:
                flush_thinking()
                flush_content()
                transcript.write("[yellow]Turn interrupted[/]")
                self._remember("status", "Turn interrupted")
        except Exception as exc:
            flush_thinking()
            flush_content()
            source = "Command" if prompt.lstrip().startswith("/") else "Provider"
            transcript.write(_transcript_text(f"✕ {source} failed: ", exc, "bold red"))
            self._remember("error", _scrub_secrets(f"{source} failed: {exc}"))
        finally:
            self._set_thinking("")
            self._stream_task = None
            self._work_started_at = None
            self._set_activity("")
            self._set_status(f"ready{self._usage_suffix()}")
            if prompt.strip() == "/new" or prompt.strip().lower().startswith("/resume "):
                await self._sync_active_session()
        if input_request is not None:
            from .user_input_screen import UserInputScreen

            async def answered(value: str | None) -> None:
                if value is not None:
                    from merlin_contracts.user_input import ANSWER_PREFIX, InputResponse

                    response = InputResponse.model_validate_json(value[len(ANSWER_PREFIX) :])
                    labels = {
                        question.id: {option.id: option.label for option in question.options}
                        for question in input_request.questions
                    }
                    chosen = " · ".join(
                        ", ".join(
                            [
                                *(labels[question_id][identity] for identity in answer.option_ids),
                                answer.text,
                            ]
                        ).strip(", ")
                        for question_id, answer in response.answers.items()
                    )
                    transcript.write(Text("● " + chosen, style="cyan"))
                    self._remember("user", value)
                    await self._start_backend(value, show_assistant=True)

            self.push_screen(
                UserInputScreen(input_request, context=input_context),
                answered,
            )
        for request_id, command in operator_commands:
            marker = f"Operator command dispatched: {request_id}"
            dispatched = getattr(self, "_operator_command_ids", set())
            if request_id in dispatched or any(
                entry.text == marker for entry in self._transcript_entries
            ):
                continue
            dispatched.add(request_id)
            self._operator_command_ids = dispatched
            self._remember("status", marker)
            try:
                await self._run_command(command)
                next_stream = self._stream_task
                if next_stream is not None and next_stream is not asyncio.current_task():
                    await next_stream
            except Exception as exc:
                transcript.write(_transcript_text("✕ Chat command failed: ", exc, "bold red"))

    def _reset_new_session_view(self) -> None:
        """Clear session-local UI only after the backend confirms creation."""
        self.query_one("#transcript", LiveTranscript).clear()
        self.query_one("#transcript", LiveTranscript).remove_class("visible")
        self.query_one("#welcome", Static).remove_class("hidden")
        self._transcript_entries.clear()
        self._pending_choice = ()
        self._pending_choice_default = ""
        self._pending_choice_kind = ""
        self._pending_profile_answer = ""
        self._pending_campaign_review = None
        self._active_saturation_request = None
        self._activity_history.clear()
        self._usage_summary = ""
        self._scan_summary_report = None
        self._swarm_rendered = ""
        self._last_swarm_campaigns = (None, ())
        view = self.query_one("#swarm-live", LiveSwarmLog)
        view.animating = False
        view.clear()
        view.remove_class("visible")
        self.query_one("#scan-actions").remove_class("visible")
        self.query_one("#scan-next").remove_class("visible")
        self._save_client_state("")

    def _offer_structured_choice(self, assistant_text: str) -> None:
        """Turn known operator decisions into keyboard-friendly numbered choices."""
        normalized = " ".join(assistant_text.lower().split())
        if "campaign profile" not in normalized:
            return
        if not all(
            profile in normalized
            for profile in ("recommended", "exhaustive", "exploration", "quickscan")
        ):
            return
        self._pending_choice = (
            ("Quickscan [default]", "quickscan"),
            ("Recommended", "recommended"),
            ("Exhaustive", "exhaustive"),
            ("Exploration / learning", "exploration"),
        )
        self._pending_choice_default = "quickscan"
        self._pending_choice_kind = "profile"
        if (
            "1. quickscan [default]" in normalized
            and "2. recommended" in normalized
            and "3. exhaustive" in normalized
            and "4. exploration / learning" in normalized
            and "press enter or 1" in normalized
        ):
            return
        from .kernel_backend import _CAMPAIGN_PROFILE_PROMPT

        self.query_one("#transcript", RichLog).write(Text(_CAMPAIGN_PROFILE_PROMPT))

    async def _run_command(self, command_line: str) -> None:
        command = command_line.split(maxsplit=1)[0]
        if command_line.strip() == "/web":
            try:
                from merlin_web.tui_bridge import open_workspace

                await open_workspace(self)
                self.notify("Current session opened in browser. Keep this terminal running.")
            except ImportError:
                self.notify(
                    "Web package unavailable. Install merlin-web and build its frontend.",
                    severity="error",
                )
            except Exception as exc:
                self.notify(f"Could not open web workspace: {exc}", severity="error")
            return
        if command_line.strip() == "/quit":
            self.exit()
            return
        if command_line.strip() == "/interrupt":
            await self.action_interrupt()
            return
        if command_line.strip() == "/clear":
            self.query_one("#transcript", RichLog).clear()
            self._transcript_entries.clear()
            self._save_client_state("")
            return
        if command_line.strip() == "/setup endpoint":
            self.push_screen(EndpointScreen(), self._save_model_endpoint)
            return
        if command_line.strip() == "/setup jev":
            self._pick_jev(_JEV_ENABLE_LABEL)
            return
        if command == "/setup" and command_line.strip() == "/setup":
            self._begin_provider_setup()
            return
        if command == "/model" and command_line.strip() == "/model":
            self.push_screen(await _configured_model_screen(self._model_name), self._save_model)
            return
        if command_line.strip() == "/doctor":
            await self._show_doctor()
            return
        if command_line.strip() == "/doctor config":
            await self._show_safe_config()
            return
        if command_line.strip() == "/logs":
            await self._show_operational_rows("Logs", self.backend.list_logs)
            return
        if command_line.strip() == "/history":
            await self._show_operational_rows("Session history", self.backend.list_history)
            return
        if command_line.strip() == "/context":
            await self._show_conversation_context()
            return
        if command_line.strip() == "/library":
            await self._show_library()
            return
        if command_line.strip() == "/sessions":
            await self._show_sessions()
            return
        if command_line.strip() == "/resume":
            await self._show_resume()
            return
        if command == "/pause":
            values = command_line.split()
            if len(values) == 3 and values[1] == "campaign":
                await self._pause_campaign(values[2])
                return
            self.query_one("#transcript", RichLog).write("[bold red]✕ Usage: /pause campaign ID[/]")
            return
        if command == "/resume" and len(command_line.split()) == 3:
            values = command_line.split()
            if values[1] == "campaign":
                await self._resume_campaign(values[2])
                return
        if command == "/targets" and command_line.strip() == "/targets":
            await self._show_targets()
            return
        if command == "/skills" and command_line.strip() == "/skills":
            await self._show_skills()
            return
        if command == "/mcp" and command_line.strip() == "/mcp":
            await self._show_mcp()
            return
        if command_line.strip() == "/mcp remote":
            self.push_screen(RemoteMcpScreen(), self._save_remote_mcp)
            return
        if command == "/agents" and command_line.strip() == "/agents":
            await self._show_agents()
            return
        if command_line.strip().lower() == "/agents kill all":
            await self._cancel_campaign("*")
            return
        if command == "/tasks" and command_line.strip() == "/tasks":
            await self._show_tasks()
            return
        if command_line.strip() == "/campaigns":
            await self._show_campaigns()
            return
        if command == "/cancel":
            values = command_line.split()
            if values == ["/cancel", "all"]:
                await self._cancel_campaign("*")
                return
            if len(values) == 3 and values[1] == "campaign":
                await self._cancel_campaign(values[2])
                return
            if len(values) == 3 and values[1] == "task":
                await self._cancel_task(values[2])
                return
            self.query_one("#transcript", RichLog).write(
                "[bold red]✕ Usage: /cancel all|campaign ID|task ID[/]"
            )
            return
        if command == "/campaign":
            values = command_line.split()
            if len(values) >= 4 and values[1] == "message":
                await self._guide_campaign(values[2], " ".join(values[3:]))
                return
            if len(values) == 3 and values[1] == "status":
                await self._show_campaign_status(values[2])
                return
            if values[1:2] == ["cancel"] and len(values) <= 3:
                await self._cancel_campaign(values[2] if len(values) == 3 else "*")
                return
            if len(values) == 3 and values[1] == "pause":
                await self._pause_campaign(values[2])
                return
            if len(values) == 3 and values[1] == "resume":
                await self._resume_campaign(values[2])
                return
            if len(values) >= 4 and values[1] == "saturation":
                additional_waves: int | None = None
                if values[3] == "limit":
                    if len(values) != 5 or not values[4].isdigit():
                        self.query_one("#transcript", RichLog).write(
                            "[bold red]✕ Usage: /campaign saturation ID limit N[/]"
                        )
                        return
                    additional_waves = int(values[4])
                elif len(values) != 4 or values[3] not in {"stop", "continue"}:
                    self.query_one("#transcript", RichLog).write(
                        "[bold red]✕ Usage: /campaign saturation ID stop|continue|limit N[/]"
                    )
                    return
                await self._decide_campaign_saturation(
                    values[2], values[3], additional_waves=additional_waves
                )
                return
            self.query_one("#transcript", RichLog).write(
                "[bold red]✕ Usage: /campaign status ID|message ID GUIDANCE|"
                "cancel [ID|all]|pause ID|resume ID[/]"
            )
            return
        if command_line.strip() == "/lessons":
            await self._show_lessons()
            return
        if command == "/findings" and command_line.strip() == "/findings":
            await self._show_findings()
            return
        if command == "/report" and command_line.strip() == "/report":
            await self._show_reports()
            return
        if command == "/permissions" and command_line.strip() == "/permissions":
            await self._show_permissions()
            return
        if command == "/promotion" and command_line.strip() == "/promotion":
            await self._show_promotions()
            return
        if command == "/browser" and command_line.strip() == "/browser":
            await self._show_browser()
            return
        if command == "/mode":
            values = command_line.split()
            if len(values) == 1:
                self.push_screen(
                    ChoiceScreen("Permission mode", ("prompt", "auto", "yolo")),
                    self._mode_choice,
                )
            elif len(values) == 2:
                await self._change_permission_mode(values[1])
            else:
                self.query_one("#transcript", RichLog).write(
                    "[bold red]✕ Usage: /mode [prompt|auto|yolo][/]"
                )
            return
        if command == "/memory":
            values = command_line.split()
            if len(values) == 1:
                await self._show_memory()
                return
            if values[1] == "list" and len(values) <= 3:
                await self._show_memory(values[2] if len(values) == 3 else None)
                return
        await self._start_backend(command_line, show_assistant=False)

    async def _show_packs(self) -> None:
        try:
            packs = await self.backend.list_packs()
            self.push_screen(OverviewScreen("Attack packs", _render_packs(packs)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Packs failed: ", exc, "bold red")
            )

    async def _show_library(self) -> None:
        try:
            self.push_screen(LibraryScreen(await self.backend.list_packs()))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Library failed: ", exc, "bold red")
            )

    async def _show_targets(self) -> None:
        try:
            targets = await self.backend.list_targets()
            self.push_screen(OverviewScreen("AI targets", _render_targets(targets)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Targets failed: ", exc, "bold red")
            )

    async def _show_skills(self) -> None:
        try:
            skills = await self.backend.list_skills()
            self.push_screen(OverviewScreen("Skills", _render_skills(skills)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Skills failed: ", exc, "bold red")
            )

    async def _show_mcp(self) -> None:
        try:
            servers = await self.backend.list_mcp_servers()
            self.push_screen(OverviewScreen("MCP servers", _render_mcp_servers(servers)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ MCP failed: ", exc, "bold red")
            )

    async def on_live_swarm_log_open_details(self) -> None:
        if self._scan_summary_report is not None:
            await self._show_reports()
        else:
            await self._show_agents()

    @on(Button.Pressed, "#scan-open-html")
    async def open_scan_html(self, event: Button.Pressed) -> None:
        event.stop()
        report = self._scan_summary_report
        if report is None:
            return
        path = await asyncio.to_thread(lambda: Path(report.output).expanduser().resolve())
        if not await asyncio.to_thread(path.is_file):
            self.notify(f"Report file is unavailable: {path}", severity="error")
            return
        await asyncio.to_thread(webbrowser.open, path.as_uri())

    @on(Button.Pressed, "#scan-reports")
    async def show_scan_reports(self, event: Button.Pressed) -> None:
        event.stop()
        await self._show_reports()

    @on(Button.Pressed, ".scan-export")
    async def export_scan_report(self, event: Button.Pressed) -> None:
        event.stop()
        if (
            self._scan_summary_report is None
            or self._stream_task is not None
            and not self._stream_task.done()
        ):
            return
        format = (event.button.id or "").removeprefix("scan-export-")
        if format not in {"markdown", "json", "csv"}:
            return
        self._activate_conversation()
        await self._start_backend(f"/report --format {format}", show_assistant=False)

    @on(Button.Pressed, "#scan-recover")
    async def recover_scan(self, event: Button.Pressed) -> None:
        event.stop()
        report = self._scan_summary_report
        if report is None or event.button.disabled:
            return
        event.button.disabled = True
        await self._resume_campaign(report.run)
        await self._poll_swarm_progress()

    async def _show_agents(self) -> None:
        self.push_screen(
            AgentScreen(self.backend, self._active_session_id), self._schedule_swarm_refresh
        )

    async def _show_tasks(self) -> None:
        try:
            schedules = await self.backend.list_schedules()
            self.push_screen(OverviewScreen("Task schedules", _render_schedules(schedules)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Tasks failed: ", exc, "bold red")
            )

    async def _show_campaigns(self) -> None:
        try:
            campaigns = _campaigns_for_session(
                await self.backend.list_campaigns(), self._active_session_id
            )
            self.push_screen(CampaignScreen(campaigns, self.backend.cancel_campaign))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaigns failed: ", exc, "bold red")
            )

    async def _show_campaign_status(self, campaign_id: str) -> None:
        try:
            status = await self.backend.campaign_status(campaign_id)
            if isinstance(status, Mapping):
                decision = status.get("saturation_decision")
                if isinstance(decision, Mapping) and decision.get("state") == "awaiting_operator":
                    self._offer_saturation_choice(campaign_id, status, decision)
                    return
            self.push_screen(
                OverviewScreen(f"Campaign · {campaign_id}", _render_campaign_status(status))
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign status failed: ", exc, "bold red")
            )

    async def _poll_saturation_decisions(self) -> None:
        if self._unmounting or self._active_saturation_request is not None:
            return
        try:
            campaigns = _campaigns_for_session(
                await self.backend.list_campaigns(), self._active_session_id
            )
            for campaign in campaigns:
                if campaign.status != "executing":
                    continue
                status = await self.backend.campaign_status(campaign.id)
                if not isinstance(status, Mapping):
                    continue
                decision = status.get("saturation_decision")
                if isinstance(decision, Mapping) and decision.get("state") == "awaiting_operator":
                    self._offer_saturation_choice(campaign.id, status, decision)
                    return
        except Exception:
            # Status remains available through /campaign status; transient reconnects
            # must not create transcript noise or duplicate prompts.
            return

    async def _poll_completed_reports(self) -> None:
        """Present each newly completed durable report exactly once."""
        if self._unmounting:
            return
        transcripts = self.query("#transcript")
        try:
            reports = await self.backend.list_reports()
        except Exception:
            return
        current = {(report.run, report.output) for report in reports}
        if self._known_report_revisions is None:
            self._known_report_revisions = current
            return
        new_reports = tuple(
            report
            for report in reports
            if (report.run, report.output) not in self._known_report_revisions
        )
        self._known_report_revisions = current
        for report in new_reports:
            self._pending_report_notifications[(report.run, report.output)] = report
        if self.screen is not self.default_screen or not transcripts:
            # The startup chooser or a modal sits above the main screen. Keep
            # polling so reports completed after the launch baseline are
            # retained, then flush them once the transcript is visible again.
            # (Textual >= 8 resolves app.query against the default screen even
            # while another screen is pushed, so presence alone is not enough.)
            return
        transcript = transcripts.first(RichLog)
        pending = tuple(self._pending_report_notifications.values())
        self._pending_report_notifications.clear()
        for report in pending:
            message = Text()
            message.append("✓ Campaign report ready\n", style="bold green")
            message.append("  Campaign: ")
            message.append(report.run, style="bold")
            message.append(f"\n  State: {_report_display_status(report)}\n  Exported: ")
            message.append(report.output, style="bold")
            message.append("\n  Run ")
            message.append("/report", style="bold")
            message.append(" to view all report revisions.")
            transcript.write(message)
            self.notify(f"Campaign report exported: {report.output}")

    async def _poll_swarm_progress(self) -> None:
        """Refresh one bounded live swarm region without flooding the transcript."""
        if self._unmounting or self._swarm_refresh_lock.locked():
            return
        async with self._swarm_refresh_lock:
            views = list(self.query("#swarm-live"))
            if not views:
                return
            view = views[0]
            if not isinstance(view, RichLog):
                return
            try:
                campaigns = await self.backend.list_campaigns()
            except Exception as exc:
                # Never leave an animated, apparently live snapshot behind when
                # campaign state cannot be refreshed. Keep saved report actions.
                if isinstance(view, LiveSwarmLog):
                    view.animating = False
                cached_session, cached_campaigns = getattr(
                    self, "_last_swarm_campaigns", (None, ())
                )
                report = self._scan_summary_report
                known_campaign = (
                    cached_session == self._active_session_id
                    and any(item.status != "prepared" for item in cached_campaigns)
                ) or report is not None
                if not known_campaign:
                    self.log.warning("Initial campaign refresh deferred", type(exc).__name__)
                    self._swarm_rendered = ""
                    view.clear()
                    view.remove_class("visible")
                    return
                cached_campaign = (
                    next(
                        (
                            item
                            for item in cached_campaigns
                            if report is not None and item.id == report.run
                        ),
                        None,
                    )
                    if cached_session == self._active_session_id
                    else None
                )
                if cached_campaign is not None and report is not None:
                    rendered = _render_scan_summary(cached_campaign, report)
                else:
                    rendered = Text("Campaign status unavailable", style="bold yellow")
                rendered.append(
                    "\nStatus refresh interrupted · retrying automatically\n"
                    + _short_activity(_scrub_secrets(str(exc)) or type(exc).__name__, 180),
                    style="yellow",
                )
                self._swarm_rendered = rendered.plain
                view.clear()
                view.write(rendered)
                view.styles.height = min(16, max(5, rendered.plain.count("\n") + 3))
                view.add_class("visible")
                return
            session_campaigns = _campaigns_for_session(campaigns, self._active_session_id)
            self._last_swarm_campaigns = (self._active_session_id, session_campaigns)
            active_campaigns = tuple(
                campaign
                for campaign in _current_campaigns(session_campaigns)
                if campaign.status != "prepared"
            )
            if active_campaigns:
                self._operator_preparing_session = ""
            # Inventory is newest-updated first. A new preparation owns the
            # current conversation even though its pre-start dock is hidden.
            # Do not fall back to an earlier campaign's completed report.
            if not active_campaigns and (
                getattr(self, "_operator_preparing_session", None) == self._active_session_id
                or (session_campaigns and session_campaigns[0].status == "prepared")
            ):
                self._scan_summary_report = None
                self._swarm_rendered = ""
                view.clear()
                view.remove_class("visible")
                if isinstance(view, LiveSwarmLog):
                    view.animating = False
                self.query_one("#scan-actions").remove_class("visible")
                self.query_one("#scan-next").remove_class("visible")
                self.query_one("#scan-recover", Button).disabled = True
                return
            self.query_one("#scan-next").set_class(
                not active_campaigns
                and any(
                    item.status in {"completed", "failed", "cancelled"}
                    for item in session_campaigns
                ),
                "visible",
            )
            if isinstance(view, LiveSwarmLog):
                view.animating = bool(active_campaigns) and any(
                    campaign.status == "executing" for campaign in active_campaigns
                )
            previous_report = self._scan_summary_report
            action_rows = list(self.query("#scan-actions"))
            # Preserve report actions while awaiting the next snapshot. Hiding
            # them before list_reports completes causes a flicker on every poll.
            if active_campaigns or not any(
                item.status in {"completed", "failed", "cancelled"} for item in session_campaigns
            ):
                self._scan_summary_report = None
                self.query_one("#scan-recover", Button).disabled = True
                if action_rows:
                    action_rows[0].remove_class("visible")
            try:
                if active_campaigns:
                    try:
                        agents = await self.backend.list_agents()
                        agents_unavailable = False
                    except Exception:
                        agents = ()
                        agents_unavailable = True
                    rendered = _render_live_swarm(
                        _agents_for_campaigns(agents, active_campaigns),
                        active_campaigns,
                        self._spinner_index,
                    )
                    if agents_unavailable:
                        rendered.append("\nWorker details unavailable · retrying", style="yellow")
                else:
                    terminal = tuple(
                        campaign
                        for campaign in session_campaigns
                        if campaign.status in {"completed", "failed", "cancelled"}
                    )
                    if not terminal:
                        self._swarm_rendered = ""
                        view.clear()
                        view.remove_class("visible")
                        return
                    campaign = terminal[0]
                    try:
                        reports = await self.backend.list_reports()
                    except Exception:
                        reports = (previous_report,) if previous_report is not None else ()
                    report = next(
                        (
                            item
                            for item in reversed(reports)
                            if item.run == campaign.id and item.format == "durable-revision"
                        ),
                        None,
                    )
                    self._scan_summary_report = report
                    self.query_one("#scan-recover", Button).disabled = not (
                        campaign.status == "failed"
                        and report is not None
                        and report.stop_reason == "failed"
                    )
                    rendered = _render_scan_summary(campaign, report)
                    if action_rows:
                        action_rows[0].set_class(report is not None, "visible")
                    if report is not None:
                        self.query_one(
                            "#scan-open-html", Button
                        ).disabled = not report.output.endswith(".html")
            except (AttributeError, TypeError, ValueError):
                # Keep the timer alive across durable schema/state transitions.
                return
            plain = rendered.plain
            if plain == self._swarm_rendered and view.has_class("visible"):
                return
            self._swarm_rendered = plain
            view.styles.height = min(16, max(5, plain.count("\n") + 3))
            view.clear()
            view.write(rendered, expand=True, shrink=True)
            view.add_class("visible")

    def _offer_saturation_choice(
        self,
        campaign_id: str,
        status: Mapping[str, object],
        decision: Mapping[str, object],
    ) -> None:
        request_id = str(decision.get("request_id", ""))
        if not request_id or self._active_saturation_request is not None:
            return
        self._active_saturation_request = request_id
        incomplete = decision.get("purpose") == "coverage_extension"
        self.push_screen(
            ChoiceScreen(
                _render_saturation_choice_summary(campaign_id, status),
                (
                    (
                        "Stop with incomplete coverage and generate report"
                        if incomplete
                        else "Stop and generate report"
                    ),
                    (
                        "Replan missing coverage and continue"
                        if incomplete
                        else "Continue exhaustive assessment"
                    ),
                    "Continue with a limit…",
                ),
                default_index=0,
            ),
            lambda choice: self._saturation_choice(campaign_id, choice),
        )

    def _saturation_choice(self, campaign_id: str, choice: str | None) -> None:
        self._active_saturation_request = None
        if choice in {
            "Stop and generate report",
            "Stop with incomplete coverage and generate report",
        }:
            self._spawn(self._decide_campaign_saturation(campaign_id, "stop"))
        elif choice in {
            "Continue exhaustive assessment",
            "Replan missing coverage and continue",
        }:
            self._spawn(self._decide_campaign_saturation(campaign_id, "continue"))
        elif choice == "Continue with a limit…":
            composer = self.query_one("#composer", Composer)
            composer.text = f"/campaign saturation {campaign_id} limit "
            composer.focus()
            self.notify("Enter the maximum number of additional waves, then press Enter")

    async def _decide_campaign_saturation(
        self,
        campaign_id: str,
        decision: str,
        *,
        additional_waves: int | None = None,
    ) -> None:
        try:
            await self.backend.decide_campaign_saturation(campaign_id, decision, additional_waves)
            self.query_one("#transcript", RichLog).write(
                f"[green]✓ Exhaustive campaign decision recorded: {decision}[/]"
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign decision failed: ", exc, "bold red")
            )

    async def _cancel_campaign(self, campaign_id: str) -> None:
        try:
            selected = "*" if campaign_id == "all" else campaign_id
            count = await self.backend.cancel_campaign(selected)
            self.query_one("#transcript", RichLog).write(
                f"[yellow]Stopped {count} campaign(s) in this session.[/]"
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign cancellation failed: ", exc, "bold red")
            )

    async def _cancel_task(self, task_id: str) -> None:
        try:
            selected = await self.backend.cancel_task(task_id)
            self.query_one("#transcript", RichLog).write(
                _transcript_text("Stopped task ", f"{selected}.", "yellow")
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Task cancellation failed: ", exc, "bold red")
            )

    async def _pause_campaign(self, campaign_id: str) -> None:
        try:
            await self.backend.pause_campaign(campaign_id)
            self.query_one("#transcript", RichLog).write(
                _transcript_text("Paused campaign ", f"{campaign_id}.", "yellow")
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign pause failed: ", exc, "bold red")
            )

    async def _guide_campaign(self, campaign_id: str, message: str) -> None:
        try:
            await self.backend.guide_campaign(campaign_id, message)
            self.query_one("#transcript", RichLog).write(
                _transcript_text(
                    "Guidance queued for campaign ",
                    f"{campaign_id}'s next lead boundary.",
                    "green",
                )
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign guidance failed: ", exc, "bold red")
            )

    async def _resume_campaign(self, campaign_id: str) -> None:
        try:
            await self.backend.resume_campaign(campaign_id)
            self.query_one("#transcript", RichLog).write(
                _transcript_text("Resumed campaign ", f"{campaign_id}.", "green")
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Campaign resume failed: ", exc, "bold red")
            )

    async def _show_lessons(self) -> None:
        try:
            lessons = await self.backend.list_lessons()
            self.push_screen(OverviewScreen("Campaign lessons", _render_lessons(lessons)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Lessons failed: ", exc, "bold red")
            )

    async def _show_findings(self) -> None:
        try:
            self.push_screen(FindingsScreen(await self.backend.list_findings()))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Findings failed: ", exc, "bold red")
            )

    async def _show_reports(self) -> None:
        try:
            reports = await self.backend.list_reports()
            self.push_screen(ReportScreen(self.backend, reports))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Reports failed: ", exc, "bold red")
            )

    async def _show_permissions(self) -> None:
        try:
            rules = await self.backend.list_permission_rules()
            history = await self.backend.list_permission_history()
            self.push_screen(
                OverviewScreen(
                    "Permissions", _render_permissions(self._permission_mode, rules, history)
                )
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Permissions failed: ", exc, "bold red")
            )

    async def _show_promotions(self) -> None:
        try:
            candidates = await self.backend.list_promotion_candidates()
            history = await self.backend.list_promotion_history()
            self.push_screen(
                OverviewScreen(
                    "Candidate evaluation and promotion",
                    _render_promotions(candidates, history),
                )
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Promotions failed: ", exc, "bold red")
            )

    async def _show_browser(self) -> None:
        try:
            contexts = await self.backend.list_browser_contexts()
            self.push_screen(BrowserScreen(contexts, self.backend.takeover_browser_context))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Browser failed: ", exc, "bold red")
            )

    async def _show_memory(self, status: str | None = None) -> None:
        try:
            records = await self.backend.list_memory_records(status)
            title = "Portable memory" + (f" · {status}" if status else "")
            self.push_screen(OverviewScreen(title, _render_memory(records)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Memory failed: ", exc, "bold red")
            )

    async def _show_doctor(self) -> None:
        try:
            healthy, checks = await self.backend.run_doctor()
            state = "healthy" if healthy else "action required"
            self._set_status(f"doctor · {state}")
            self.push_screen(OverviewScreen(f"Doctor · {state}", _render_doctor(checks)))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Doctor failed: ", exc, "bold red")
            )

    async def _show_operational_rows(
        self,
        title: str,
        loader: Callable[[int], Awaitable[tuple[OperationalEntryView, ...]]],
    ) -> None:
        try:
            self.push_screen(OverviewScreen(title, _render_operational_rows(await loader(100))))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text(f"✕ {title} failed: ", exc, "bold red")
            )

    async def _show_safe_config(self) -> None:
        try:
            self.push_screen(
                OverviewScreen(
                    "Non-secret configuration export",
                    await self.backend.export_safe_config(),
                )
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Config export failed: ", exc, "bold red")
            )

    async def _show_conversation_context(self) -> None:
        try:
            self.push_screen(
                OverviewScreen(
                    "Conversation context provenance",
                    _render_context_provenance(await self.backend.conversation_context()),
                )
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Context provenance failed: ", exc, "bold red")
            )

    async def _show_resume(self) -> None:
        try:
            self.push_screen(
                ResumeScreen(await self.backend.list_resume_sessions()), self._resume_choice
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Resume failed: ", exc, "bold red")
            )

    async def _show_sessions(self) -> None:
        await self._open_sessions_page()

    async def _open_sessions_page(self, *, offset: int = 0, query: str = "") -> None:
        try:
            page = await self.backend.list_resume_session_page(limit=20, offset=offset, query=query)
            self.push_screen(
                SessionBrowserScreen(page),
                lambda action: self._finish_sessions_browse(action, page),
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Sessions failed: ", exc, "bold red")
            )

    def _finish_sessions_browse(self, action: str | None, page: ResumeSessionPage) -> None:
        if action == "next":
            self._spawn(self._open_sessions_page(offset=page.offset + page.limit, query=page.query))
        elif action == "previous":
            self._spawn(
                self._open_sessions_page(offset=max(0, page.offset - page.limit), query=page.query)
            )
        elif isinstance(action, str) and action.startswith("search:"):
            self._spawn(self._open_sessions_page(query=action.removeprefix("search:").strip()))
        elif isinstance(action, str) and action.startswith("resume:"):
            self._resume_choice(action.removeprefix("resume:"))

    def _resume_choice(self, session_id: str | None) -> None:
        if session_id:
            self._active_session_id = session_id
            self.query_one("#transcript", RichLog).write(
                f"[dim]Resuming durable session {session_id}…[/]"
            )
            self._spawn(self._start_backend(f"/resume {session_id}", show_assistant=False))

    async def _sync_active_session(self) -> None:
        try:
            context = await self.backend.conversation_context()
        except Exception:
            return
        session_id = (
            context.get("session_id", "")
            if isinstance(context, Mapping)
            else getattr(context, "session_id", "")
        )
        self._active_session_id = str(session_id or "")

    async def _start_backend(self, text: str, *, show_assistant: bool) -> None:
        self._assistant_pending = show_assistant
        self._activity_history.clear()
        self._active_thinking = ""
        self._work_started_at = time.monotonic()
        self._work_label = "Working"
        self._usage_summary = ""
        self._refresh_work_indicator()
        self._stream_task = self._spawn(self._stream(text))

    def _record_activity(self, text: str) -> None:
        self._activity_history.append(text)
        del self._activity_history[:-100]

    def _refresh_work_indicator(self) -> None:
        if self._work_started_at is None:
            return
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        frame = frames[self._spinner_index % len(frames)]
        self._spinner_index += 1
        elapsed = int(time.monotonic() - self._work_started_at)
        activity = (
            f"{frame} {self._work_label} ({elapsed}s · Esc to interrupt)\n"
            "Tip: /agents · /agent message ID TEXT · /pause campaign ID · "
            "/cancel task ID"
        )
        self._set_activity(activity)
        self._set_status(f"WORKING {elapsed}s{self._usage_suffix()}")

    def _usage_suffix(self) -> str:
        return f" · {self._usage_summary}" if self._usage_summary else ""

    def _set_activity(self, text: str) -> None:
        try:
            activity = self.query_one("#activity", Static)
        except NoMatches:
            return
        activity.update(text)
        activity.set_class(bool(text), "visible")

    def _set_thinking(self, text: str) -> None:
        try:
            transcript = self.query_one("#transcript", LiveTranscript)
        except NoMatches:
            return
        if text:
            transcript.replace_live(
                Text(_thinking_preview(text, transcript.region.width), style="dim")
            )
        else:
            transcript.discard_live()

    def action_details(self) -> None:
        if isinstance(self.screen, ActiveWorkScreen):
            self.screen.dismiss(None)
            return
        details = self._active_work_details()
        if not details:
            self.notify("No active-work details yet", severity="information")
            return
        self.push_screen(ActiveWorkScreen(self._active_work_details))

    def _active_work_details(self) -> str:
        details = [_activity_detail_view(item) for item in self._activity_history]
        if self._active_thinking:
            details.append(f"• Thinking · {self._active_thinking}")
        return "\n".join(details)

    def action_palette(self) -> None:
        self.push_screen(ChoiceScreen("Command palette", COMMANDS), self._palette_choice)

    def action_copy_transcript(self) -> None:
        transcript = self.query_one("#transcript", LiveTranscript)
        text = "\n".join(line.text.rstrip() for line in transcript.lines).rstrip()
        if not text:
            self.notify("Transcript is empty", severity="information")
            return
        self.push_screen(TranscriptCopyScreen(text))

    def action_scroll_up(self) -> None:
        self.query_one("#transcript", RichLog).scroll_page_up(animate=False)

    def action_scroll_down(self) -> None:
        self.query_one("#transcript", RichLog).scroll_page_down(animate=False)

    def _remember(self, kind: str, text: str) -> None:
        self._transcript_entries.append(TranscriptEntry(kind, text))
        del self._transcript_entries[:-500]
        try:
            draft = self.query_one("#composer", Composer).text
        except NoMatches:
            draft = ""
        self._save_client_state(draft)

    def _save_client_state(self, draft: str) -> None:
        try:
            self._client_state.save(draft, tuple(self._transcript_entries))
        except OSError:
            # UI persistence must never take down an active assessment.
            pass

    def _palette_choice(self, choice: str | None) -> None:
        if choice:
            self.query_one("#composer", Composer).text = choice
            self.query_one("#composer", Composer).focus()

    def _save_provider(self, setup: ProviderSetup | None) -> None:
        if setup is not None:
            self._spawn(self._configure_provider(setup))

    def _save_model_endpoint(self, setup: EndpointSetup | None) -> None:
        if setup is not None:
            self._spawn(self._configure_model_endpoint(setup))

    async def _configure_model_endpoint(self, setup: EndpointSetup) -> None:
        try:
            message = await self.backend.configure_model_endpoint(
                setup.model, setup.base_url, setup.api_key
            )
            self.query_one("#transcript", RichLog).write(_transcript_text("", message, "green"))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Endpoint setup failed: ", exc, "bold red")
            )

    def _begin_provider_setup(self) -> None:
        from .provider_catalog import PROVIDER_ROUTES

        choices = tuple(route.label for route in PROVIDER_ROUTES) + (
            "Codex App Server — configure model routing",
            "Claude CLI — configure Campaign Lead routing",
        )
        jev_choice = "JEV — judgment API key"
        try:
            import shutil

            from merlin_config import CredentialStore, load_config

            config = load_config()
            if config.judgments.jev_token and config.judgments.jev_token.strip():
                jev_choice = "✓ JEV — judgment API key · API key saved"
            choices = _marked_provider_choices(
                choices,
                config.values.get("provider", {}),
                CredentialStore().load(),
                {name for name in ("codex", "claude", "ollama", "lms") if shutil.which(name)},
            )
        except (OSError, TypeError, ValueError):
            pass
        self.push_screen(
            SetupChoiceScreen("Setup — provider or judgment model", (jev_choice, *choices)),
            self._pick_provider,
        )

    def _pick_provider(self, choice: str | None) -> None:
        from .provider_catalog import PROVIDER_ROUTES

        if choice is None:
            return
        choice = choice.removeprefix("✓ ").split(" · ", 1)[0].split("  ✓", 1)[0]
        if choice == "JEV — judgment API key":
            self._pick_jev(_JEV_ENABLE_LABEL)
            return
        if choice in {
            "Codex App Server — configure model routing",
            "Claude CLI — configure Campaign Lead routing",
        }:
            self._spawn(self._run_command("/model"))
            return
        route = next(route for route in PROVIDER_ROUTES if route.label == choice)
        effort = "default"
        try:
            from merlin_config import load_config

            provider = load_config().values.get("provider", {})
            if isinstance(provider, Mapping):
                effort = str(provider.get("effort", "default"))
        except (OSError, TypeError, ValueError):
            pass
        self.push_screen(SetupScreen(route, route.base_url, "", effort), self._save_provider)

    def _save_remote_mcp(self, setup: RemoteMcpSetup | None) -> None:
        if setup is not None:
            self._spawn(self._configure_remote_mcp(setup))

    async def _configure_remote_mcp(self, setup: RemoteMcpSetup) -> None:
        try:
            message = await self.backend.configure_remote_mcp(
                setup.name, setup.url, setup.credential_handle, setup.secret
            )
            self.query_one("#transcript", RichLog).write(_transcript_text("", message, "green"))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Remote MCP setup failed: ", exc, "bold red")
            )

    async def _configure_provider(self, setup: ProviderSetup) -> None:
        try:
            message = await self.backend.configure_provider(
                setup.base_url,
                setup.model,
                setup.api_key,
                setup.vision,
                setup.context_window_tokens,
                setup.effort,
            )
            self.query_one("#transcript", RichLog).write(_transcript_text("", message, "green"))
            self.push_screen(
                ChoiceScreen(
                    "Enable the judgment model (Jev) for refusal grading and triage?",
                    (_JEV_SKIP_LABEL, _JEV_ENABLE_LABEL),
                ),
                self._pick_jev,
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Setup failed: ", exc, "bold red")
            )

    def _pick_jev(self, choice: str | None) -> None:
        if choice is None:
            return
        if choice == _JEV_ENABLE_LABEL:
            token_configured = False
            try:
                from merlin_config import load_config

                token_configured = bool(load_config().judgments.jev_token)
            except (OSError, TypeError, ValueError):
                pass
            self.push_screen(JevSetupScreen(token_configured), self._save_jev)
            return
        try:
            from .configuration import ProviderConfiguration

            ProviderConfiguration().save_jev_enabled(False)
            self.query_one("#transcript", RichLog).write(
                _transcript_text("", "Judgment model (Jev) stays off — LLM judging is used.", "dim")
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Setup failed: ", exc, "bold red")
            )

    def _save_jev(self, setup: JevSetup | None) -> None:
        if setup is not None:
            self._spawn(self._configure_jev(setup))

    async def _configure_jev(self, setup: JevSetup) -> None:
        try:
            api_key = setup.api_key.strip()
            if not api_key:
                message = "Judgment model (Jev) API key unchanged."
            else:
                from .configuration import ProviderConfiguration

                await asyncio.to_thread(ProviderConfiguration().save_jev_token, api_key)
                await asyncio.to_thread(ProviderConfiguration().save_jev_enabled, True)
                health = await _check_jev_health(api_key)
                message = f"Saved judgment model (Jev) API key. {health}"
            self.query_one("#transcript", RichLog).write(_transcript_text("", message, "green"))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Setup failed: ", exc, "bold red")
            )

    def _save_model(self, setup: ModelSetup | None) -> None:
        if setup is not None:
            self._spawn(self._change_model(setup))

    async def _change_model(self, setup: ModelSetup) -> None:
        try:
            message = await self.backend.set_model_routing(
                setup.worker_model,
                setup.worker_transport,
                setup.heterogeneous,
                setup.lead_model,
                setup.lead_transport,
                setup.recon_model,
                setup.recon_transport,
                setup.validator_model,
                setup.validator_transport,
                setup.adversarial_explorer_model,
                setup.adversarial_explorer_transport,
                setup.role_efforts,
                judgments_jev_enabled=setup.judgments_jev_enabled,
            )
            apply_saved = getattr(self.backend, "apply_saved_model_routing", None)
            if callable(apply_saved):
                message = f"{message} {await apply_saved()}"
            self.query_one("#transcript", RichLog).write(_transcript_text("", message, "green"))
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Model failed: ", exc, "bold red")
            )

    async def action_interrupt(self) -> None:
        stream_task = self._stream_task
        previous_label = self._work_label
        if stream_task and not stream_task.done():
            self._work_label = "Interrupting"
            self._refresh_work_indicator()
        # A finished/disconnected UI stream does not prove the daemon turn is idle.
        # Keep reading until the backend acknowledges cancellation; closing the local
        # reader alone leaves a daemon-owned reconnaissance turn running.
        try:
            await self.backend.interrupt()
        except Exception as exc:
            self._activate_conversation()
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Interrupt failed: ", exc, "bold red")
            )
            self._remember("error", _scrub_secrets(f"Interrupt failed: {exc}"))
            if stream_task and not stream_task.done():
                self._work_label = previous_label
                self._refresh_work_indicator()
            return
        if stream_task and not stream_task.done():
            stream_task.cancel()

    async def confirm_tool_call(
        self,
        spec: Any,
        context: Any,
        arguments: Mapping[str, Any],
    ) -> bool:
        """Resolve one broker confirmation without remembering the decision."""
        del context
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def resolved(allowed: bool) -> None:
            if not result.done():
                result.set_result(allowed)

        self.push_screen(ToolConfirmationScreen(str(spec.name), arguments), resolved)
        return await result

    async def resolve_permission(
        self, request: Any, *, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        from merlin_permissions import PermissionDecision

        async with self._permission_lock:
            result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._permission_result = result
            self._permission_previous_focus = self.focused
            scope = request.scope
            qualifiers = [f"Workspace: {_permission_single_line(scope.workspace_root)}"]
            if scope.plugin_id:
                qualifiers.append(f"Plugin: {_permission_single_line(scope.plugin_id)}")
            if scope.target:
                qualifiers.append(f"Target: {_permission_single_line(scope.target)}")
            if scope.operation_group:
                qualifiers.append(f"Scope: {_permission_single_line(scope.operation_group)}")
            self.query_one("#permission-scope", Static).update(
                f"Effect: {request.effect.value} · "
                f"Tool: {_permission_single_line(scope.tool_name)}\n"
                + " · ".join(qualifiers)
                + "\nReason: Approval required by current permission settings."
            )
            details = self.query_one("#permission-details-scroll", VerticalScroll)
            invocation = self.query_one("#permission-details", Static)
            invocation.display = arguments is not None
            invocation.update(
                _render_permission_details(scope.tool_name, scope.workspace_root, arguments)
                if arguments is not None
                else ""
            )
            details.scroll_home(animate=False)
            shell = scope.tool_name == "shell_command"
            command_available = (
                shell
                and arguments is not None
                and arguments.get("__permission_command_scope") is True
            )
            self.query_one("#permission-allow-command-session", Button).display = command_available
            self.query_one("#permission-deny-once", Button).label = (
                "4. Deny" if command_available else "3. Deny"
            )
            for decision, verb, number in (
                ("allow", "Allow", 3 if command_available else 2),
                ("deny", "Deny", 5 if command_available else 4),
            ):
                self.query_one(f"#permission-{decision}-session", Button).label = (
                    f"{number}. {verb} all commands here for this session"
                    if shell
                    else f"{number}. {verb} this scope for this session"
                )
            self.query_one("#permission-drawer", Vertical).add_class("visible")
            self.query_one("#composer", Composer).disabled = True
            self.query_one("#permission-allow-once", Button).focus()
            try:
                return PermissionDecision(await result)
            finally:
                if self._permission_result is result:
                    self._finish_permission("deny_once")

    @on(Button.Pressed)
    def resolve_inline_permission(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if self._permission_result is None or not button_id.startswith("permission-"):
            return
        self._finish_permission(button_id.removeprefix("permission-").replace("-", "_"))

    def on_key(self, event: events.Key) -> None:
        if self._permission_result is None:
            return
        if event.key in {"pageup", "pagedown"}:
            event.prevent_default()
            event.stop()
            details = self.query_one("#permission-details-scroll", VerticalScroll)
            if event.key == "pageup":
                details.scroll_page_up(animate=False)
            else:
                details.scroll_page_down(animate=False)
            return
        if event.key in {"up", "down"}:
            event.prevent_default()
            event.stop()
            buttons = [
                button for button in self.query("#permission-drawer Button") if button.display
            ]
            current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
            offset = -1 if event.key == "up" else 1
            buttons[(current + offset) % len(buttons)].focus()
            return
        shortcuts = {
            "1": "allow_once",
            "a": "allow_once",
            "2": "allow_session",
            "l": "allow_session",
            "3": "deny_once",
            "d": "deny_once",
            "escape": "deny_once",
            "4": "deny_session",
            "n": "deny_session",
        }
        if self.query_one("#permission-allow-command-session", Button).display:
            shortcuts.update(
                {
                    "2": "allow_command_session",
                    "3": "allow_session",
                    "4": "deny_once",
                    "5": "deny_session",
                }
            )
        if decision := shortcuts.get(event.key):
            event.prevent_default()
            event.stop()
            self._finish_permission(decision)

    def _finish_permission(self, decision: str) -> None:
        result, self._permission_result = self._permission_result, None
        if result is None:
            return
        self.query_one("#permission-drawer", Vertical).remove_class("visible")
        composer = self.query_one("#composer", Composer)
        composer.disabled = False
        if self._permission_previous_focus is not None:
            self._permission_previous_focus.focus()
        else:
            composer.focus()
        self._permission_previous_focus = None
        if not result.done():
            result.set_result(decision)

    async def confirm_permission_tool(
        self, spec: Any, context: Any, arguments: Mapping[str, Any]
    ) -> Any:
        from merlin_permissions import PermissionEffect, PermissionRequest, PermissionScope

        effect_value = arguments.get("__permission_effect", getattr(spec, "effect", "unknown"))
        try:
            effect = PermissionEffect(str(effect_value))
        except ValueError:
            effect = PermissionEffect.UNKNOWN
        name = str(spec.name)
        plugin_id = arguments.get("__permission_plugin")
        if plugin_id is None and name.startswith("mcp__"):
            plugin_id = name.split("__", 2)[1]
        target = (
            arguments.get("__permission_target")
            or arguments.get("target")
            or arguments.get("target_id")
            or getattr(context, "target_id", None)
        )
        return await self.resolve_permission(
            PermissionRequest(
                effect,
                PermissionScope(
                    str(context.session_id),
                    name,
                    str(context.workspace_root),
                    plugin_id=plugin_id,
                    target=str(target) if target is not None else None,
                    operation_group=(
                        str(arguments["__permission_group"])
                        if arguments.get("__permission_group") is not None
                        else None
                    ),
                ),
            ),
            arguments=arguments,
        )

    async def _load_permission_mode(self) -> None:
        for attempt in range(2):
            try:
                self._permission_mode = await self.backend.get_permission_mode()
                break
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(0.1)
        self._set_status("ready")

    async def _load_runtime_status(self) -> None:
        for attempt in range(2):
            try:
                status = await self.backend.get_runtime_status()
                if isinstance(status, Mapping):
                    provider = str(status.get("provider", "provider"))
                    self._model_name = str(status.get("model", "unknown"))
                    self._effort = str(status.get("effort", "default"))
                else:
                    provider = str(getattr(status, "provider", "provider"))
                    self._model_name = str(getattr(status, "model", "unknown"))
                    self._effort = str(getattr(status, "effort", "default"))
                parsed = urlparse(provider)
                self._provider_name = parsed.hostname or provider
                break
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(0.1)
        self._set_status("ready")

    def _mode_choice(self, mode: str | None) -> None:
        if mode:
            self._spawn(self._change_permission_mode(mode))

    async def _change_permission_mode(self, mode: str) -> None:
        try:
            self._permission_mode = await self.backend.set_permission_mode(mode)
            self._set_status("ready")
            self.query_one("#transcript", RichLog).write(
                f"[yellow]Permission mode: {self._permission_mode}[/]"
            )
        except Exception as exc:
            self.query_one("#transcript", RichLog).write(
                _transcript_text("✕ Mode failed: ", exc, "bold red")
            )

    def _set_status(self, text: str) -> None:
        mode = (
            self._permission_mode.upper()
            if self._permission_mode == "yolo"
            else self._permission_mode
        )
        warning = " ⚠" if self._permission_mode == "yolo" else ""
        try:
            self.query_one("#status", Static).update(
                f"●  {text} · mode {mode}{warning} · daemon attached"
            )
        except NoMatches:
            return

    async def on_unmount(self) -> None:
        self._unmounting = True
        try:
            draft = self.query_one("#composer", Composer).text
        except NoMatches:
            draft = ""
        self._save_client_state(draft)
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        console = getattr(self, "_web_console", None)
        if console:
            await console.close()
        shutdown = getattr(self.backend, "shutdown", None)
        cleanup = shutdown if callable(shutdown) else self.backend.close
        cleanup_task = asyncio.create_task(cleanup())
        try:
            async with asyncio.timeout(_SHUTDOWN_TIMEOUT_SECONDS):
                await asyncio.shield(cleanup_task)
        except TimeoutError:
            cleanup_task.cancel()
            try:
                async with asyncio.timeout(_SHUTDOWN_CANCEL_SECONDS):
                    await asyncio.gather(cleanup_task, return_exceptions=True)
            except TimeoutError:
                pass
            self.log.error(
                f"runtime cleanup exceeded the {_SHUTDOWN_TIMEOUT_SECONDS:.1f} "
                "second shutdown deadline"
            )
        except Exception as exc:
            self.log.error("runtime cleanup reported errors: " + _scrub_secrets(str(exc)))


def _entry_style(kind: str) -> str:
    return {
        "user": "bold cyan",
        "content": "",
        "status": "dim",
        "error": "bold red",
    }.get(kind, "dim")


_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "key",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "private_key",
)


def _redact_sensitive(value: Any, key: str = "") -> Any:
    if any(marker in key.casefold() for marker in _SECRET_KEY_MARKERS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_sensitive(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_redact_sensitive(item) for item in value), key=str)
    return value


def _scrub_secrets(value: str) -> str:
    scrubbed = re.sub(
        r"(?i)\b(api[-_ ]?key|access[-_ ]?key|password|passwd|secret|token|"
        r"authorization|cookie|credential)\b(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[redacted]",
        value,
    )
    scrubbed = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", scrubbed)
    return re.sub(r"(://[^:/\s]+:)[^@/\s]+(@)", r"\1[redacted]\2", scrubbed)


def _transcript_text(prefix: str, value: object, style: str) -> Text:
    rendered = Text(prefix, style=style)
    rendered.append(_scrub_secrets(str(value)))
    return rendered


def _render_packs(packs: tuple[PackView, ...]) -> str:
    if not packs:
        return "No attack packs discovered."
    sections = []
    for pack in packs:
        state = "enabled" if pack.enabled else "disabled"
        cases = ", ".join(case.title for case in pack.cases) or "none"
        runs = "\n    ".join(pack.recent_runs) or "none"
        sections.append(
            f"{pack.title} ({pack.id}) · {state}\n"
            f"  {pack.instructions}\n  Cases: {cases}\n  Recent runs: {runs}"
        )
    return "\n\n".join(sections)


def _render_targets(targets: tuple[TargetView, ...]) -> str:
    if not targets:
        return "No AI targets configured. Use /targets add to create one."
    return "\n".join(
        f"{'●' if target.selected else '○'} {target.title} ({target.id}) · {target.model}"
        for target in targets
    )


def _render_skills(skills: tuple[SkillView, ...]) -> str:
    if not skills:
        return "No skills discovered."
    rows = []
    for skill in skills:
        enabled = "enabled" if skill.enabled else "disabled"
        detail = f" · {skill.status}" if skill.status not in {"ready", enabled} else ""
        error = f"\n  Error: {skill.error}" if skill.error else ""
        marker = "●" if skill.enabled else "○"
        rows.append(f"{marker} {skill.name} · {enabled}{detail}\n  {skill.description}{error}")
    return "\n\n".join(rows)


def _render_mcp_servers(servers: tuple[McpServerView, ...]) -> str:
    if not servers:
        return "No MCP servers configured."
    rows = []
    for server in servers:
        marker = "●" if server.health == "healthy" and not server.disabled else "○"
        tools = ", ".join(server.tools) or "none discovered"
        calls = "\n    ".join(server.recent_calls) or "none"
        audit = "\n    ".join(server.recent_audit) or "none"
        stderr = f"\n  Stderr: {server.stderr}" if server.stderr else ""
        digest = server.inventory_digest or "unavailable"
        identity = server.identity or "unavailable"
        rows.append(
            f"{marker} {server.name} · {server.status} · {server.health}"
            f" · {'disabled' if server.disabled else 'enabled'}\n"
            f"  Transport: {server.transport} · Admission: {server.admission}\n"
            f"  Identity: {identity}\n  Inventory digest: {digest}\n"
            f"  Reconnect: {server.reconnect}\n  Tools: {tools}\n"
            f"  Recent calls: {calls}\n  Recent audit: {audit}{stderr}"
        )
    return "\n\n".join(rows)


def _permission_single_line(value: str) -> str:
    """Keep untrusted labels from inserting terminal controls or extra prompt lines."""
    return re.sub(
        r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]",
        lambda match: json.dumps(match[0], ensure_ascii=True)[1:-1],
        value,
    )


def _permission_display_values(value: Any) -> Any:
    """Scrub display copies, including credentials passed as argv flag/value pairs."""
    if isinstance(value, Mapping):
        return {str(key): _permission_display_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        rendered = []
        hide_next = False
        for item in value:
            rendered.append("[redacted]" if hide_next else _permission_display_values(item))
            hide_next = isinstance(item, str) and bool(
                re.fullmatch(
                    r"(?i)--?(?:api[-_]?key|access[-_]?key|password|passwd|secret|token|"
                    r"authorization|cookie|credential|private[-_]?key)",
                    item,
                )
            )
        return rendered
    if isinstance(value, str):
        if re.match(r"(?i)^(authorization|cookie|x-api-key)\s*:", value):
            return value.split(":", 1)[0] + ": [redacted]"
        assignment = re.match(
            r"(?i)^(?:--?)?(?:api[-_]?key|access[-_]?key|password|passwd|secret|token|"
            r"authorization|cookie|credential|private[-_]?key)=",
            value,
        )
        if assignment:
            return value[: assignment.end()] + "[redacted]"
        value = re.sub(
            r"(?i)\b(api[-_ ]?key|access[-_ ]?key|password|passwd|secret|token|"
            r"authorization|cookie|credential)\b(\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            r"\1\2[redacted]",
            value,
        )
        return _scrub_secrets(value)
    return value


def _render_permission_details(
    tool_name: str, workspace_root: str, arguments: Mapping[str, Any]
) -> str:
    """Show the invocation before approval; never alter the execution arguments."""
    public = {key: value for key, value in arguments.items() if not key.startswith("__permission_")}
    safe = _permission_display_values(_redact_sensitive(public))
    lines = []
    if tool_name == "shell_command":
        argv = safe.get("argv")
        if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
            # Make terminal controls visible instead of letting command text spoof the prompt.
            command = _permission_single_line(shlex.join(argv))
            lines.append(f"Command: {command}")
        cwd = arguments.get("cwd", ".")
        try:
            if not isinstance(cwd, str) or Path(cwd).is_absolute():
                raise ValueError("cwd must be relative to the workspace")
            root = Path(workspace_root).resolve(strict=True)
            directory = (root / cwd).resolve(strict=True)
            if not directory.is_relative_to(root) or not directory.is_dir():
                raise ValueError("cwd must be a directory inside the workspace")
            directory_text = json.dumps(str(directory), ensure_ascii=True)[1:-1]
            lines.append(f"Working directory: {directory_text}")
        except (OSError, RuntimeError, ValueError) as error:
            error_text = json.dumps(str(error), ensure_ascii=True)[1:-1]
            lines.append(f"Working directory: invalid ({error_text})")
    lines.append("Arguments (sensitive values redacted):")
    lines.append(json.dumps(safe, indent=2, ensure_ascii=True, default=str))
    return "\n".join(lines)


def _render_tool_arguments(arguments: Mapping[str, Any]) -> str:
    """Render useful call context while recursively redacting credentials."""
    safe = _redact_sensitive(arguments)
    try:
        return json.dumps(safe, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(tuple(safe))


def _render_recovered_campaign_review(payload: Mapping[str, object]) -> Panel:
    """Display persisted values without fabricating missing historical metadata."""
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column("Field", style="bold cyan", width=20)
    table.add_column("Saved value", overflow="fold")
    for field_name, value in (
        ("Campaign", payload["campaign_id"]),
        ("Manifest", payload["manifest_digest"]),
        ("Objective", payload["objective"]),
        ("Profile", payload["profile"]),
        *payload["review"]["recovered_fields"].items(),
    ):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        table.add_row(Text(str(field_name)), Text(rendered))
    table.add_row("Approval", "Press Enter to approve this saved plan, request changes, or cancel.")
    return Panel(
        table,
        title="Saved campaign review",
        border_style="cyan",
        subtitle="Digest verified · No workers start before approval",
    )


def _render_campaign_review(value: str) -> Panel:
    payload = json.loads(value)
    review = payload["review"]
    if "recovered_fields" in review:
        return _render_recovered_campaign_review(payload)
    budget = review["budget"]
    coverage = review["admitted_coverage"]
    languages = review["language_plan"]
    hypotheses = review["hypothesis_reserve"]
    learning = review.get("cross_campaign_learning", {})
    capabilities = review["capability_projection"]
    target = review["target_contract"]
    recon = review["recon_evidence"]

    def listing(values: object) -> str:
        if not isinstance(values, (list, tuple)) or not values:
            return "Not declared"
        return ", ".join(str(item) for item in values)

    def route_rate(detail: Mapping[str, object], key: str) -> object:
        value = detail.get(key)
        return value

    configured_routes = dict(review.get("model_routes", {}))
    default_model = str(review["provider_model"])
    role_routes = {
        "Campaign Lead": configured_routes.get("campaign-lead", default_model),
        "Recon": configured_routes.get("recon", default_model),
        "Worker": configured_routes.get("worker")
        or configured_routes.get("routine")
        or default_model,
        "Validator": configured_routes.get("validator")
        or configured_routes.get("independent-validator")
        or default_model,
        "Adversarial explorer": configured_routes.get("adversarial-explorer", default_model),
    }

    route_details = review.get("model_route_details", {})

    def details_for(role: str, model: object) -> Mapping[str, object]:
        direct = route_details.get(role)
        if isinstance(direct, Mapping):
            return direct
        return next(
            (
                detail
                for detail in route_details.values()
                if isinstance(detail, Mapping) and detail.get("model") == model
            ),
            {},
        )

    table = Table(
        show_header=False,
        box=None,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("Field", style="bold cyan", width=20, no_wrap=True)
    table.add_column("Reviewed value", overflow="fold")
    table.add_row("Campaign", str(payload["campaign_id"]))
    table.add_row("Manifest", str(payload["manifest_digest"]))
    table.add_row("Objective", str(payload["objective"]))
    table.add_row("Profile", str(payload["profile"]))
    table.add_row(
        "Model routes",
        " · ".join(f"{role}: {model}" for role, model in role_routes.items()),
    )
    table.add_row(
        "Model metadata",
        " · ".join(
            (
                f"{role}: metadata unavailable"
                if not detail.get("transport") and not detail.get("capabilities")
                else f"{role}: {detail.get('transport') or 'transport metadata unavailable'}"
                + (f"; {listing(detail.get('capabilities'))}" if detail.get("capabilities") else "")
                + (
                    f"; ${route_rate(detail, 'input_cost_per_million')}/"
                    f"${route_rate(detail, 'output_cost_per_million')} per 1M tokens"
                    if route_rate(detail, "input_cost_per_million") is not None
                    and route_rate(detail, "output_cost_per_million") is not None
                    else ""
                )
            )
            for role, model in role_routes.items()
            for detail in (details_for(role.casefold().replace(" ", "-"), model),)
        ),
    )
    table.add_row(
        "Model escalation",
        " · ".join(
            f"{role}: {' → '.join(models)}"
            for role, models in review.get("model_escalations", {}).items()
        )
        or "No automatic escalation routes",
    )
    table.add_row(
        "Target contract",
        f"{target['interface']} · {target['endpoint']} · adapter {target['adapter_id']}"
        + (f" · session: {target['session_constraint']}" if target["session_constraint"] else ""),
    )
    interface = str(target["interface"])
    api_validated = bool(target.get("api_validated"))
    selected_mode = str(
        target.get("execution_mode", "browser" if interface == "browser" else "api")
    )
    execution_mode = {
        "api": (
            "API — validated and recommended"
            if api_validated
            else "API — captured adapter; validation not proven"
        ),
        "browser": "Browser — API handoff not validated",
        "hybrid": "Hybrid — API execution with browser evidence checkpoints",
    }.get(selected_mode, f"{selected_mode} — reviewed adapter")
    table.add_row("Execution mode", execution_mode)
    table.add_row(
        "Team",
        f"{payload['campaign_lead']} Lead coordinating {payload['specialist_team']} "
        f"test specialists · up to {max(1, int(payload['maximum_concurrency']) - 1)} "
        "specialist tasks at once",
    )
    composition = review.get("specialist_composition") or payload.get("specialist_composition")
    if composition:
        table.add_row(
            "Team composition",
            f"{str(composition).capitalize()} specialist selection; the coverage profile "
            "still determines which techniques must be tested",
        )
    configured_budget = tuple(
        f"{value:,} {unit}"
        for value, unit in (
            (budget.get("turns"), "turns"),
            (budget.get("tokens"), "tokens"),
            (budget.get("seconds"), "seconds"),
            (budget.get("cost_usd"), "USD"),
        )
        if isinstance(value, (int, float))
    )
    table.add_row(
        "Campaign limits",
        " · ".join(configured_budget)
        if configured_budget
        else "No campaign-wide turn, token, time, or cost cap configured",
    )
    table.add_row(
        "Admitted coverage",
        f"{coverage['technique_count']} techniques · {listing(coverage['technique_ids'])}",
    )
    if learning:
        table.add_row(
            "Prior campaign reuse",
            f"{learning.get('skipped', 0)} skipped · "
            f"{learning.get('revalidated', 0)} revalidated · "
            f"{learning.get('new', 0)} new",
        )
    table.add_row(
        "Language plan",
        f"{languages['baseline']} baseline · {languages['policy']}"
        + (
            f" · cells: {listing(languages['language_evasion_cells'])}"
            if languages["language_evasion_cells"]
            else ""
        ),
    )
    table.add_row("Authentication", str(review["authentication"]))
    table.add_row(
        "Recon evidence",
        f"{len(recon['references'])} references"
        f" · input {recon['input_locator'] or 'not observed'}"
        f" · submit {recon['submit_action'] or 'not observed'}"
        f" · witness {recon['success_indicator'] or 'not observed'}",
    )
    table.add_row("Benign controls", listing(review["benign_controls"]))
    table.add_row(
        "Test assignments",
        " · ".join(
            f"{profile} runs: {listing(techniques)}"
            for profile, techniques in review["specialist_routing"].items()
        )
        or "Assignments will be created by the Campaign Lead",
    )
    table.add_row(
        "Hypothesis reserve",
        f"{hypotheses['required_cells']} cells · {hypotheses['rule']}",
    )
    table.add_row("Pack projection", listing(capabilities["packs"]))
    table.add_row("Skill projection", listing(capabilities["skills"]))
    table.add_row("MCP projection", listing(capabilities["mcp_tools"]))
    table.add_row("Tool projection", listing(capabilities["tools"]))
    table.add_row("Authority", listing(capabilities["authority"]))
    table.add_row("Stopping policy", str(review["stopping_policy"]))
    table.add_row("Cleanup obligations", " · ".join(review["cleanup_obligations"]))
    table.add_row("Known limitations", listing(review["known_limitations"]))
    table.add_row("Approval binding", str(review["approval_binding"]))
    available_modes = listing(target.get("execution_options"))
    if available_modes != "None admitted":
        table.add_row("Available modes", available_modes)
    approval = Text(
        "Approve, choose an available execution mode, request changes, or cancel.\n"
        "Press Enter to approve",
        style="bold",
    )
    return Panel(
        Group(table, Text(""), approval),
        title="Campaign review",
        subtitle="No workers start before approval",
        border_style="cyan",
        padding=(1, 1),
        expand=True,
    )


def _agent_state_group(state: str) -> str:
    display_state = _agent_display_state(state)
    if display_state == "needs review":
        return "uncertain"
    if display_state in {
        "completed",
        "failed",
        "cancelled",
        "quarantined",
        "interrupted",
    }:
        return "done"
    if display_state == "working":
        return "working"
    return "queued"


def _agent_display_state(state: str) -> str:
    """Keep terminal failures and uncertain evaluation distinct in the UI."""
    normalized = state.lower().replace("-", "_").replace(" ", "_")
    for label, markers in (
        ("needs review", ("evaluation_uncertain", "evaluator_uncertain")),
        ("quarantined", ("quarantined", "quarantine")),
        ("interrupted", ("interrupted", "aborted", "stopped")),
        ("cancelled", ("cancelled", "canceled")),
        ("failed", ("failed", "failure", "error")),
        ("completed", ("completed", "confirmed", "refuted", "succeeded")),
        ("working", ("running", "executing", "leased", "working")),
        ("queued", ("queued", "pending", "prepared")),
    ):
        if any(marker in normalized for marker in markers):
            return label
    return normalized.replace("_", " ") or "unknown"


def _ordered_agents(agents: tuple[AgentView, ...]) -> tuple[AgentView, ...]:
    priority = {"working": 0, "queued": 1, "uncertain": 2, "done": 3}
    return tuple(
        sorted(
            agents,
            key=lambda agent: priority[_agent_state_group(agent.latest_event)],
        )
    )


def _current_campaigns(campaigns: tuple[CampaignView, ...]) -> tuple[CampaignView, ...]:
    running = tuple(
        campaign for campaign in campaigns if campaign.status in {"executing", "paused"}
    )
    if running:
        return running[:1]
    prepared = tuple(campaign for campaign in campaigns if campaign.status == "prepared")
    return prepared[:1]


def _campaigns_for_session(
    campaigns: tuple[CampaignView, ...], session_id: str
) -> tuple[CampaignView, ...]:
    """Keep daemon-global campaign state out of unrelated conversations."""
    if not session_id:
        return ()
    return tuple(campaign for campaign in campaigns if campaign.session_id == session_id)


def _agents_for_campaigns(
    agents: tuple[AgentView, ...], campaigns: tuple[CampaignView, ...]
) -> tuple[AgentView, ...]:
    """Scope global durable agent rows to the campaign shown by the monitor."""
    if not campaigns:
        return ()
    campaign_ids = tuple(campaign.id for campaign in campaigns)

    def belongs_to_campaign(agent: AgentView) -> bool:
        if agent.campaign_id is not None:
            return agent.campaign_id in campaign_ids
        if agent.identity_provenance == "durable":
            return False
        name_parts = agent.name.replace(":", "/").split("/")
        return any(
            any(
                name_parts[index : index + len(campaign_parts)] == campaign_parts
                for index in range(len(name_parts) - len(campaign_parts) + 1)
            )
            for value in campaign_ids
            for campaign_parts in (value.split("/"),)
        )

    return tuple(agent for agent in agents if belongs_to_campaign(agent))


def _agent_display_name(agent: AgentView) -> str:
    action = agent.name.rstrip("/").rsplit("/", maxsplit=1)[-1]
    role = agent.profile or "specialist"
    return role if action in {"", role} else f"{role} · {action}"


def _specialist_instance_key(agent: AgentView) -> str:
    """Best-effort legacy grouping until the backend exposes a durable specialist ID."""
    if agent.specialist_instance_id is not None:
        return f"{agent.identity_provenance}:{agent.specialist_instance_id}"
    parts = agent.name.rstrip("/").split("/")
    for index, part in enumerate(parts):
        if part.startswith("agenda-"):
            return "legacy-agenda:" + "/".join(parts[: index + 1])
    return f"legacy-agent:{agent.profile or 'unclassified'}:{agent.name}"


def _specialist_groups(
    agents: tuple[AgentView, ...],
) -> tuple[tuple[str, tuple[AgentView, ...]], ...]:
    grouped: dict[str, list[AgentView]] = {}
    for agent in _ordered_agents(agents):
        grouped.setdefault(_specialist_instance_key(agent), []).append(agent)
    return tuple((key, tuple(assignments)) for key, assignments in grouped.items())


def _specialist_group_keys(agents: tuple[AgentView, ...]) -> tuple[str, ...]:
    return tuple(key for key, _assignments in _specialist_groups(agents))


def _specialist_group_state(assignments: tuple[AgentView, ...]) -> str:
    states = tuple(_agent_display_state(agent.latest_event) for agent in assignments)
    for state in ("working", "needs review", "quarantined", "interrupted", "failed"):
        if state in states:
            return state
    if "queued" in states:
        return "queued"
    if len(set(states)) == 1:
        return states[0]
    return "mixed outcomes"


def _inferred_task_group_label(assignments: tuple[AgentView, ...]) -> str:
    representative = next(
        (agent for agent in assignments if _agent_stage(agent) != "control"), assignments[0]
    )
    if representative.profile == "Unassigned/structural tasks":
        return "Target discovery and preparation"
    techniques = tuple(
        item.strip() for item in (representative.technique or "").split(",") if item.strip()
    )
    if techniques:
        leaf = techniques[0].split("@", maxsplit=1)[0].rstrip("/").rsplit("/", maxsplit=1)[-1]
        subject = leaf.replace("-", " ").replace("_", " ").strip()
        if subject.lower().startswith("agent "):
            subject = subject[len("agent ") :].strip()
    elif representative.profile:
        subject = representative.profile.replace("-", " ").strip()
    else:
        subject = representative.name.rstrip("/").rsplit("/", maxsplit=1)[-1]
        subject = subject.replace("-", " ").strip()
    return subject.capitalize() or "Unclassified"


def _specialist_group_line(content: str | Text) -> int:
    plain = content.plain if isinstance(content, Text) else content
    return next(
        (line for line, value in enumerate(plain.splitlines()) if value.startswith("› ")),
        0,
    )


def _specialist_instance_label(agent: AgentView) -> str:
    key = _specialist_instance_key(agent)
    instance = key.removeprefix("legacy-agenda:").rsplit("/", maxsplit=1)[-1]
    if instance.startswith("agenda-"):
        instance = instance.removeprefix("agenda-")[:8]
    return f"{agent.profile or 'unclassified'} · {instance}"


def _agent_stage(agent: AgentView) -> str:
    if "/" not in agent.name and agent.profile:
        return f"{agent.profile} · {agent.name}"
    return agent.name.rstrip("/").rsplit("/", maxsplit=1)[-1]


def _human_specialist_name(agent: AgentView) -> str:
    if agent.profile and not agent.profile.startswith("dynamic-") and "/" not in agent.name:
        profile = agent.profile.replace("-", " ").strip()
        instance = agent.name.replace("-", " ").strip()
        return f"{profile} · {instance} specialist"
    techniques = tuple(item.strip() for item in (agent.technique or "").split(",") if item.strip())
    if techniques:
        leaf = techniques[0].split("@", maxsplit=1)[0].rstrip("/").rsplit("/", maxsplit=1)[-1]
        label = leaf.replace("-", " ").strip().capitalize()
        if len(techniques) > 1:
            return f"{label} coverage specialist"
        return f"{label} specialist"
    if not agent.profile and "/" not in agent.name:
        instance = agent.name.replace("-", " ").strip().capitalize()
        return instance if instance.endswith(" specialist") else f"{instance} specialist"
    profile = (agent.profile or "specialist").replace("-", " ").strip().capitalize()
    return profile if profile.endswith("specialist") else f"{profile} specialist"


def _short_activity(line: str, limit: int = 120) -> str:
    cleaned = " ".join(line.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _render_structured_choice_content(value: str) -> Text:
    """Add restrained hierarchy to known operator-choice prompts."""
    normalized = value.lower()
    if (
        "choose the campaign profile" not in normalized
        and "specialist composition" not in normalized
    ):
        return Text(value)
    number_styles = {
        "1": "bold cyan",
        "2": "bold green",
        "3": "bold magenta",
        "4": "bold yellow",
    }
    rendered = Text()
    for index, line in enumerate(value.splitlines(keepends=True)):
        stripped = line.lstrip()
        number = stripped[:1]
        if len(stripped) > 2 and number in number_styles and stripped[1:2] == ".":
            prefix_length = len(line) - len(stripped)
            rendered.append(line[:prefix_length])
            rendered.append(stripped[:2], style=number_styles[number])
            rendered.append(stripped[2:])
        elif line.startswith("Press Enter"):
            rendered.append("────────────────────────────────────────────────\n", style="dim")
            rendered.append(line, style="dim")
        elif line.startswith(("   ", "Times are estimates")):
            rendered.append(line, style="dim")
        else:
            rendered.append(line, style="bold" if index == 0 else None)
    start = 0
    while (match := rendered.plain.find("[default]", start)) >= 0:
        rendered.stylize("bold yellow", match, match + len("[default]"))
        start = match + len("[default]")
    return rendered


def _render_operator_choices(choices: tuple[tuple[str, str], ...]) -> Text:
    """Render campaign-review choices with the standard selection palette."""
    styles = ("bold cyan", "bold green", "bold magenta", "bold yellow")
    rendered = Text("Choose ", style="bold")
    for offset, (label, _value) in enumerate(choices):
        if offset:
            rendered.append(" or ", style="bold")
        rendered.append(str(offset + 1), style=styles[offset % len(styles)])
        rendered.append(" (")
        default_suffix = " [default]"
        if label.endswith(default_suffix):
            rendered.append(label[: -len(default_suffix)])
            rendered.append(default_suffix, style="bold yellow")
        else:
            rendered.append(label)
        rendered.append(")")
    rendered.append("\n────────────────────────────────────────────────\n", style="dim")
    rendered.append("Press Enter for the default.", style="dim")
    return rendered


def _render_scan_summary(campaign: CampaignView, report: ReportView | None) -> Text:
    if report is None:
        title = (
            "Scan stopped"
            if campaign.status == "cancelled"
            else "Scan partially completed"
            if campaign.status == "failed"
            else "Scan ended"
        )
    elif report.stop_reason in {"operator_stopped", "interrupted"}:
        title = "Scan stopped"
    elif report.report_state in {"incomplete", "provisional"} or report.stop_reason == "failed":
        title = "Scan partially completed"
    else:
        title = "Scan complete"
    rendered = Text()
    rendered.append(f"{title} — {_short_activity(campaign.objective, 100)}\n", style="bold cyan")
    rendered.append(f"Campaign: {campaign.id}\n", style="dim")
    if report is None:
        state = "Partially completed" if campaign.status == "failed" else campaign.status
        rendered.append(f"State: {state} · report pending\n", style="yellow")
        rendered.append(
            "Waiting for the durable report. /report shows saved revisions.", style="dim"
        )
        return rendered
    reason = {
        "saturated": "Coverage complete",
        "completed_with_limitations": "Finished with limitations",
        "capped": "Authorized budget reached",
        "operator_stopped": "Stopped by you",
        "interrupted": "Interrupted",
        "failed": "Execution stopped after an error; completed work is retained",
    }.get(report.stop_reason, report.stop_reason.replace("_", " ") or report.status)
    rendered.append(f"Reason: {reason}\n")
    rendered.append(
        f"Findings: {report.finding_count} · Target exchanges: {report.exchange_count}\n"
    )
    # Keep the export above potentially lengthy coverage and limitations so it
    # stays visible in the bounded main-screen region.
    rendered.append("HTML report: ", style="bold")
    rendered.append(report.output + "\n", style="cyan")
    rendered.append("Saved automatically · other formats available below\n", style="dim")
    rendered.append(f"Cleanup: {report.cleanup_status or report.status}\n")
    if report.coverage:
        coverage = " · ".join(f"{key.replace('_', ' ')}: {value}" for key, value in report.coverage)
        rendered.append(f"Coverage: {coverage}\n")
    else:
        rendered.append("Coverage: not recorded in this revision\n", style="dim")
    for limitation in report.limitations[:2]:
        preview = limitation.replace("status 'failed'", "an execution error")
        rendered.append(f"Limitation: {_short_activity(preview, 160)}\n", style="yellow")
    return rendered


def _campaign_wave_label(campaign: CampaignView) -> str:
    number = getattr(campaign, "wave_number", None)
    if number is None:
        return ""
    kind = getattr(campaign, "wave_kind", "").replace("_", " ").replace("-", " ")
    return f"Wave {number} · {kind or 'execution'}"


def _render_live_swarm(
    agents: tuple[AgentView, ...], campaigns: tuple[CampaignView, ...], spinner_index: int
) -> Text:
    ordered = _ordered_agents(agents)
    counts = {group: 0 for group in ("working", "queued", "uncertain", "done")}
    outcomes: dict[str, int] = {}
    for agent in ordered:
        counts[_agent_state_group(agent.latest_event)] += 1
        state = _agent_display_state(agent.latest_event)
        outcomes[state] = outcomes.get(state, 0) + 1
    total = len(ordered)
    finished = counts["done"]
    inferred_groups = _specialist_groups(ordered)
    group_count = len(inferred_groups)
    width = 18
    filled = 0 if total == 0 else round(width * finished / total)
    bar = "█" * filled + "░" * (width - filled)
    spinner = ("◐", "◓", "◑", "◒")[spinner_index % 4]
    paused = bool(campaigns) and all(campaign.status == "paused" for campaign in campaigns)
    title = campaigns[0].objective if campaigns else "Active campaign"
    if len(title) > 84:
        title = title[:83].rstrip() + "…"

    rendered = Text()
    rendered.append(f"Agent swarm — {title}\n", style="bold cyan")
    lead = campaigns[0] if campaigns else None
    wave_label = _campaign_wave_label(lead) if lead else ""
    reviewing = bool(lead and lead.status == "executing") and (
        getattr(lead, "wave_status", "") in {"completed", "failed", "cancelled"}
        or (total > 0 and finished == total)
    )
    if wave_label:
        rendered.append(
            f"{wave_label} · campaign totals below\n",
            style="cyan",
        )
    if paused:
        rendered.append("◎ Paused  ", style="bold yellow")
    elif reviewing:
        rendered.append(f"{spinner} Reviewing wave results…  ", style="bold yellow")
    elif lead and lead.lead_activity == "finishing current work":
        rendered.append(f"{spinner} Finishing current work…  ", style="bold yellow")
    else:
        rendered.append(f"{spinner} Working…  ", style="bold yellow")
    rendered.append(bar, style="cyan")
    rendered.append(
        f"  {group_count} task group{'s' if group_count != 1 else ''} · "
        f"tasks {finished}/{total} finished · {counts['working']} working · "
        f"{counts['queued']} queued · {counts['uncertain']} needs review\n"
    )
    if campaigns:
        lead = campaigns[0]
        lead_state = {
            "prepared": "awaiting approval",
            "executing": "coordinating task groups",
            "paused": "paused",
        }.get(lead.status, lead.status)
        if reviewing:
            lead_state = "reviewing results · deciding next wave or completion"
        if lead.lead_activity:
            lead_state = lead.lead_activity
        rendered.append(f"├─ Campaign Lead · {lead_state}\n", style="bold")
    working = tuple(
        agent for agent in ordered if _agent_state_group(agent.latest_event) == "working"
    )
    queued = tuple(agent for agent in ordered if _agent_state_group(agent.latest_event) == "queued")
    terminal = tuple(
        agent
        for agent in ordered
        if _agent_display_state(agent.latest_event) in {"completed", "failed", "interrupted"}
    )
    visible = (working + terminal)[:4]
    for agent in visible:
        state = _agent_display_state(agent.latest_event)
        glyph, style = _agent_state_chip(state)
        number = ordered.index(agent) + 1
        rendered.append(f"├─ {number:02d} {glyph} {state} · ", style=style)
        rendered.append(_short_activity(_agent_assignment_label(agent), 76), style="bold")
        rendered.append("\n")
        latest = agent.output[-1] if agent.output else agent.activity[-1] if agent.activity else ""
        if not latest and state == "failed":
            latest = agent.result or "No failure reason recorded"
        elif not latest:
            latest = "Waiting for a worker activity update"
        rendered.append(f"│  Latest: {_short_activity(latest, 110)}\n", style="dim")
    if len(working) > len(visible):
        rendered.append(f"├─ +{len(working) - len(visible)} more working agents\n", style="dim")
    if queued:
        labels = tuple(_short_activity(agent.technique or agent.task, 44) for agent in queued[:2])
        remainder = f" +{len(queued) - len(labels)}" if len(queued) > len(labels) else ""
        rendered.append(f"├─ Next up · {len(queued)} queued — {', '.join(labels)}{remainder}\n")
    completed = outcomes.get("completed", 0)
    exceptional = tuple(
        (state, count)
        for state in (
            "failed",
            "cancelled",
            "quarantined",
            "interrupted",
        )
        if (count := outcomes.get(state, 0))
    )
    terminal_lines = ([("completed", completed)] if completed else []) + list(exceptional)
    if not terminal_lines:
        rendered.append("└─ /agents for full activity", style="dim")
    for offset, (state, count) in enumerate(terminal_lines):
        prefix = "└─" if offset == len(terminal_lines) - 1 else "├─"
        marker = "✓" if state == "completed" else "!"
        style = "green" if state == "completed" else "yellow"
        rendered.append(f"{prefix} {marker} {count} {state}", style=style)
        if offset != len(terminal_lines) - 1:
            rendered.append("\n")
    rendered.append("\nClick swarm or /agents · ↑/↓ select · Enter inspect", style="dim")
    return rendered


def _agent_state_chip(state: str) -> tuple[str, str]:
    """Glyph and style for an agent or group display state."""
    return {
        "working": ("●", "green"),
        "queued": ("○", "dim"),
        "completed": ("✓", "green"),
        "failed": ("✕", "red"),
        "cancelled": ("✕", "dim"),
        "quarantined": ("⚠", "yellow"),
        "needs review": ("⚠", "yellow"),
        "interrupted": ("◌", "yellow"),
    }.get(state, ("○", "dim"))


def _agent_assignment_label(agent: AgentView) -> str:
    task = agent.task.strip()
    if agent.technique.startswith("capability/") and task.startswith("Perform exactly one"):
        surface = agent.technique.removeprefix("capability/").replace("_", " ").replace("/", " · ")
        return f"Test target capability: {surface}"
    if task and not task.startswith(("campaign/", "worker:", "assignment/")):
        return _short_activity(task.split("\n\n", 1)[0], 180)
    technique = agent.technique or task.rsplit("/", 1)[-1]
    if technique.startswith("capability/"):
        surface = technique.removeprefix("capability/").replace("_", " ")
        return f"Discover target support for {surface}"
    return technique.replace("_", " ").replace("-", " ") or "Waiting for assignment"


def _render_agent_inspector(agent: AgentView, index: int, total: int) -> Text:
    rendered = Text()
    rendered.append(
        f"Agent {index + 1:02d} of {total} · {total} tasks · {_agent_stage(agent)}\n", style="bold"
    )
    state = _agent_display_state(agent.latest_event)
    glyph, style = _agent_state_chip(state)
    retrying = state == "failed" and any("retry scheduled" in line for line in agent.activity)
    rendered.append(
        f"{glyph} {state}" + (" · retry scheduled" if retrying else "") + "\n", style=style
    )
    rendered.append(f"{_inferred_task_group_label((agent,))} · {state}\n", style="bold")
    rendered.append("\nWorking on\n", style="bold")
    rendered.append("  " + _agent_assignment_label(agent) + "\n")
    if agent.technique:
        rendered.append(f"Technique: {agent.technique}\n", style="cyan")
    rendered.append(f"Model: {agent.provider} · {agent.elapsed}\n", style="dim")
    rendered.append("\nCurrent activity\n", style="bold")
    for line in agent.activity:
        if state == "failed" and agent.result and agent.result in line:
            continue
        rendered.append(f"  {line}\n")
    if not agent.activity:
        rendered.append("No worker activity has been recorded yet.\n", style="dim")
    if state == "failed":
        rendered.append(
            f"\nFailure: {agent.result or 'No reason was recorded.'}\n", style="#ff8290"
        )
    rendered.append("\nWorker output\n", style="bold cyan")
    if agent.output:
        for line in agent.output:
            rendered.append(line + "\n\n")
    elif agent.result and state != "failed":
        rendered.append(agent.result + "\n")
    else:
        rendered.append("No worker output has been recorded yet.\n", style="dim")
    if agent.task != _agent_assignment_label(agent):
        rendered.append("\nFull assignment\n", style="bold")
        rendered.append(agent.task + "\n")
    rendered.append("\n↑/↓ select agent · PageUp/PageDown scroll · Esc back", style="dim")
    return rendered


def _render_agent_group_detail(selected: tuple[AgentView, ...]) -> Text:
    representative = next(
        (agent for agent in selected if _agent_stage(agent) != "control"), selected[0]
    )
    dynamic = representative.profile.startswith("dynamic-")
    state = _specialist_group_state(selected)
    glyph, style = _agent_state_chip(state)
    rendered = Text()
    rendered.append(f"{glyph} ", style=style)
    rendered.append(_inferred_task_group_label(selected), style="bold")
    suffix = f" · {state} · {len(selected)} task{'s' if len(selected) != 1 else ''}"
    if dynamic:
        suffix += " · temporary group"
    rendered.append(f"{suffix}\n", style="dim")
    rendered.append(f"{'─' * 60}\n", style="dim")
    priority = {"working": 0, "failed": 1, "interrupted": 2, "queued": 3}
    for agent in sorted(
        selected, key=lambda item: priority.get(_agent_display_state(item.latest_event), 4)
    ):
        agent_state = _agent_display_state(agent.latest_event)
        agent_glyph, agent_style = _agent_state_chip(agent_state)
        rendered.append("\n")
        rendered.append(_agent_stage(agent), style="bold")
        rendered.append(f"  {agent_glyph} {agent_state}\n", style=agent_style)
        rendered.append("Working on\n")
        rendered.append(f"  {_agent_assignment_label(agent)}\n")
        if agent.provider:
            rendered.append(f"Model: {agent.provider}\n", style="dim")
        activity_rows = tuple(getattr(agent, "activity", ()))
        if activity_rows:
            rendered.append("Current activity\n", style="bold")
            for line in activity_rows[-3:]:
                rendered.append(f"  {_short_activity(line, 180)}\n", style="dim")
        elif agent_state == "working":
            rendered.append("Waiting for the worker's next activity update.\n", style="dim")
        if agent_state == "failed":
            rendered.append("Failure: ", style="bold #ff8290")
            rendered.append(
                f"{agent.result or 'No failure reason was recorded.'}\n", style="#ff8290"
            )
        elif agent.result and agent_state == "completed":
            rendered.append(f"Outcome: {_short_activity(agent.result, 240)}\n", style="green")
    rendered.append("\nEsc: back to swarm · drawer refreshes every 2 seconds", style="dim")
    return rendered


def _render_agents(
    agents: tuple[AgentView, ...],
    campaigns: tuple[CampaignView, ...] = (),
    selected_group_key: str | None = None,
    cursor_group_key: str | None = None,
) -> Text:
    agents = _ordered_agents(agents)
    if not agents and not campaigns:
        return Text("No campaigns or specialist agents recorded.")
    specialist_instances = _specialist_groups(agents)
    if cursor_group_key is None and specialist_instances:
        cursor_group_key = specialist_instances[0][0]
    selected = next(
        (assignments for key, assignments in specialist_instances if key == selected_group_key),
        (),
    )
    if selected:
        return _render_agent_group_detail(selected)
    groups = {group: 0 for group in ("working", "queued", "uncertain", "done")}
    outcomes: dict[str, int] = {}
    for agent in agents:
        groups[_agent_state_group(agent.latest_event)] += 1
        state = _agent_display_state(agent.latest_event)
        outcomes[state] = outcomes.get(state, 0) + 1
    chips: list[tuple[str, str, str]] = [
        ("●", f"{groups['working']} working", "green"),
        ("○", f"{groups['queued']} queued", "dim"),
    ]
    if outcomes.get("completed"):
        chips.append(("✓", f"{outcomes['completed']} done", "green"))
    if groups["uncertain"]:
        chips.append(("⚠", f"{groups['uncertain']} needs review", "yellow"))
    for state, glyph, style in (
        ("failed", "✕", "#ff8290"),
        ("cancelled", "✕", "dim"),
        ("quarantined", "⚠", "yellow"),
        ("interrupted", "◌", "yellow"),
    ):
        if outcomes.get(state):
            chips.append((glyph, f"{outcomes[state]} {state}", style))
    rendered = Text()
    rendered.append("Agents\n", style="bold")
    for index, (glyph, label, style) in enumerate(chips):
        if index:
            rendered.append("   ")
        rendered.append(f"{glyph} {label}", style=style)
    rendered.append("\n")
    active_campaigns = _current_campaigns(campaigns)
    if agents and not campaigns:
        rendered.append("\nPast task records · not linked to a visible campaign\n", style="dim")
    for campaign in active_campaigns:
        lead_glyph, lead_style = {
            "executing": ("●", "green"),
            "paused": ("◐", "yellow"),
        }.get(campaign.status, ("○", "dim"))
        rendered.append("\n")
        rendered.append(f"{lead_glyph} ", style=lead_style)
        rendered.append("Campaign Lead", style="bold")
        rendered.append(f" — {campaign.status}\n", style=lead_style)
        rendered.append(f"  {_short_activity(campaign.objective, 120)}\n", style="dim")
        rendered.append(
            f"  {_short_activity(campaign.id, 32)} · {campaign.profile}"
            f" · {_display_digest(campaign.manifest_digest)}\n",
            style="dim",
        )
    for key, assignments in specialist_instances:
        state = _specialist_group_state(assignments)
        glyph, style = _agent_state_chip(state)
        is_cursor = key == cursor_group_key
        marker = "›" if is_cursor else " "
        rendered.append(f"\n{marker} ")
        rendered.append(f"{glyph} ", style=style)
        rendered.append(
            _inferred_task_group_label(assignments),
            style="bold cyan" if is_cursor else "bold",
        )
        rendered.append(
            f" · {len(assignments)} task{'s' if len(assignments) != 1 else ''}\n", style="dim"
        )
        active = tuple(
            agent
            for agent in assignments
            if _agent_display_state(agent.latest_event) in {"working", "failed", "interrupted"}
        )
        shown = active[:3] or assignments[:3]
        for agent in shown:
            agent_state = _agent_display_state(agent.latest_event)
            rendered.append(f"    {_agent_stage(agent)} · {agent_state}\n")
            rendered.append(f"    {_agent_assignment_label(agent)}\n")
            if agent.technique and "/" not in agent.technique:
                rendered.append(f"    {agent.technique} · {agent.elapsed}\n", style="dim")
            if agent_state == "failed":
                rendered.append(
                    f"    Failure: {agent.result or 'No reason recorded'}\n", style="#ff8290"
                )
            elif agent.activity:
                rendered.append(
                    f"    Now: {_short_activity(agent.activity[-1], 100)}\n", style="dim"
                )
        if len(active) > len(shown):
            rendered.append(
                f"    +{len(active) - len(shown)} active tasks · Enter for details\n", style="dim"
            )
    rendered.append("\n↑/↓ move · Enter inspect · Esc close", style="dim")
    return rendered


def _render_schedules(schedules: tuple[ScheduleView, ...]) -> str:
    if not schedules:
        return "No task schedules recorded."
    rows = []
    for schedule in schedules:
        lines = [
            f"{schedule.id} · {schedule.status}",
            f"  Graph: {schedule.graph_digest}",
        ]
        for node in schedule.nodes:
            dependencies = ", ".join(node.dependencies) or "none"
            tools = ", ".join(node.tools) or "none"
            guidance = ", ".join(node.guidance) or "none"
            skills = ", ".join(node.skills) or "none"
            lines.extend(
                (
                    f"  Task {node.id} · {node.status}",
                    f"    Profile: {node.profile} · Technique: {node.technique}",
                    f"    Target adapter: {node.target_adapter}",
                    f"    Authority: {node.authority}",
                    f"    Budgets: {node.budgets}",
                    f"    Provider route: {node.provider_route or 'none'}",
                    f"    Tools: {tools} · Guidance: {guidance} · Skills: {skills}",
                    f"    Dependencies: {dependencies}",
                )
            )
            for attempt in node.attempts:
                mailbox = ", ".join(attempt.mailbox) or "empty"
                result = attempt.result or "pending"
                lines.extend(
                    (
                        f"    Attempt {attempt.id} · {attempt.status} · worker: {attempt.worker}",
                        f"      Lease: generation {attempt.lease_generation}"
                        f" · expires {attempt.lease_expiry}",
                        f"      Retries: {attempt.retries}",
                        f"      Cleanup: {attempt.cleanup} · Quarantine: {attempt.quarantine}",
                        f"      Mailbox: {mailbox}",
                        f"      Latest event: {attempt.latest_event or 'none'}",
                        f"      Result: {result}",
                    )
                )
        rows.append("\n".join(lines))
    return "\n\n".join(rows)


def _campaign_display_status(status: object) -> str:
    return "Partially completed" if status == "failed" else str(status)


def _report_display_status(report: ReportView) -> str:
    if report.stop_reason == "failed" or report.report_state in {"incomplete", "provisional"}:
        return "Partially completed · report ready"
    return report.status


def _render_campaigns(campaigns: tuple[CampaignView, ...]) -> str:
    if not campaigns:
        return "No campaigns recorded for this conversation."
    return "\n\n".join(
        f"{campaign.id} · {_campaign_display_status(campaign.status)} · {campaign.profile}\n"
        + (f"  {_campaign_wave_label(campaign)}\n" if _campaign_wave_label(campaign) else "")
        + f"  {campaign.objective}\n  Manifest: {_display_digest(campaign.manifest_digest)}"
        for campaign in campaigns
    )


def _dynamic_creation_reason(record: Mapping[str, object]) -> str:
    """Render why a dynamic specialist exists, tolerating pre-projection backends."""
    reason = record.get("creation_reason")
    if isinstance(reason, Mapping):
        template = reason.get("template_id")
        if template:
            version = reason.get("template_version")
            suffix = f" v{version}" if version not in (None, "") else ""
            return f" · template {template}{suffix}"
        refs = reason.get("evidence_refs")
        if isinstance(refs, (tuple, list)) and refs:
            return f" · lead evidence: {', '.join(str(item) for item in refs)}"
        return ""
    if reason:
        return f" · {reason}"
    template = record.get("template_id")
    if template:
        version = record.get("template_version")
        suffix = f" v{version}" if version not in (None, "") else ""
        return f" · template {template}{suffix}"
    refs = record.get("evidence_refs")
    if isinstance(refs, (tuple, list)) and refs:
        return f" · lead evidence: {', '.join(str(item) for item in refs)}"
    return ""


def _render_campaign_status(status: object) -> str:
    if not isinstance(status, Mapping) and hasattr(status, "__dict__"):
        status = vars(status)
    if not isinstance(status, Mapping):
        return str(status)
    lines = [
        f"Status: {_campaign_display_status(status.get('status', 'unknown'))}",
        "Stop reason: "
        + (
            "Execution stopped early; completed work is retained"
            if status.get("stop_reason") == "failed"
            else str(status.get("stop_reason") or "not stopped")
        ),
        f"Manifest: {_display_digest(status.get('manifest_digest', 'unknown'))}",
    ]
    composition = status.get("specialist_composition") or status.get("composition_policy")
    if composition:
        lines.append(f"Specialist composition: {composition}")
    pace = status.get("pace")
    if isinstance(pace, Mapping):
        pace_lines = [
            "",
            "Pace",
            f"  Elapsed: {_format_duration(pace.get('elapsed_seconds'))}"
            f" · Waves completed: {pace.get('waves_completed', 0)}"
            f" · Avg wave: {_format_duration(pace.get('avg_wave_seconds'))}",
        ]
        current_wave = pace.get("current_wave")
        if isinstance(current_wave, Mapping):
            last_avg = _format_duration(pace.get("last_wave_avg_task_seconds"))
            pace_lines.append(
                f"  Wave {current_wave.get('wave', '?')}: "
                f"{current_wave.get('tasks_completed', 0)}/"
                f"{current_wave.get('tasks_total', 0)} tasks"
                f" · avg task {_format_duration(current_wave.get('avg_task_seconds'))}"
                f" (last wave {last_avg})"
                f" · wave elapsed {_format_duration(current_wave.get('elapsed_seconds'))}"
            )
            eta = current_wave.get("eta_seconds")
            if isinstance(eta, (int, float)):
                pace_lines.append(f"  Wave ETA: ~{_format_duration(eta)}")
            else:
                pace_lines.append("  Wave ETA: estimating (awaiting first completed task)")
        min_remaining = pace.get("min_remaining_seconds")
        if isinstance(min_remaining, (int, float)):
            pace_lines.append(
                f"  At least ~{_format_duration(min_remaining)} more if this is the"
                " last wave (lower bound — the Lead decides further waves,"
                " no total ETA)"
            )
        lines.extend(pace_lines)
    saturation = status.get("saturation")
    if isinstance(saturation, Mapping):
        attempted = int(saturation.get("hypothesis_cells_attempted", 0))
        required = int(saturation.get("hypothesis_cells_required", 0))
        terminal = int(saturation.get("terminal_tasks", 0))
        total = int(saturation.get("total_tasks", 0))
        catalogue_total = sum(
            int(saturation.get(field, 0))
            for field in (
                "catalogue_cells_confirmed",
                "catalogue_cells_refuted",
                "catalogue_cells_blocked",
                "catalogue_cells_terminal_blocked",
                "catalogue_cells_unclear",
                "catalogue_cells_unsearched",
            )
        )
        lines.extend(
            (
                "",
                "Adaptive coverage frontier",
                f"  Eligible catalogue cells: {catalogue_total:,} logical candidates"
                " · scheduled in bounded adaptive batches",
                f"  Quiet waves: {saturation.get('no_new_findings_waves', 0)}/3",
                "  Catalogue techniques attempted: "
                f"{saturation.get('attempted_techniques', 0)}/"
                f"{saturation.get('admitted_techniques', 0)} · "
                f"{saturation.get('untested_techniques', 0)} untested",
                "  Catalogue outcomes: "
                f"{saturation.get('catalogue_cells_confirmed', 0)} confirmed · "
                f"{saturation.get('catalogue_cells_refuted', 0)} refuted · "
                f"{saturation.get('catalogue_cells_blocked', 0)} blocked · "
                f"{saturation.get('catalogue_cells_terminal_blocked', 0)} "
                "terminally blocked · "
                f"{saturation.get('catalogue_cells_unclear', 0)} unclear · "
                f"{saturation.get('catalogue_cells_unsearched', 0)} unsearched",
                f"  Catalogue owners: {len(saturation.get('catalogue_cell_owners', ()))} assigned",
                f"  Terminal tasks: {terminal}/{total}",
                f"  Hypothesis cells attempted: {attempted}/{required}",
                "  Hypothesis outcomes: "
                f"{saturation.get('hypothesis_cells_confirmed', 0)} confirmed · "
                f"{saturation.get('hypothesis_cells_refuted', 0)} refuted · "
                f"{saturation.get('hypothesis_cells_blocked', 0)} blocked · "
                f"{saturation.get('hypothesis_cells_unclear', 0)} unclear · "
                f"{saturation.get('hypothesis_cells_unsearched', 0)} unsearched",
                "  Hypothesis owners: "
                f"{len(saturation.get('hypothesis_cell_owners', ()))} assigned",
                f"  Awaiting validation: {saturation.get('awaiting_validation', 0)}",
                f"  Ready for operator decision: {'yes' if saturation.get('ready') else 'no'}",
            )
        )
    specialists = status.get("dynamic_specialists", ())
    if isinstance(specialists, (tuple, list)) and specialists:
        retired = sum(
            1
            for record in specialists
            if isinstance(record, Mapping) and record.get("state") == "retired"
        )
        lines.extend(
            (
                "",
                "Dynamic specialists (Lead-admitted, TTL-bound)",
                f"  Active: {len(specialists) - retired} · Retired: {retired}",
            )
        )
        for record in specialists:
            if not isinstance(record, Mapping):
                continue
            state = str(record.get("state", "unknown"))
            if state == "retired":
                lifecycle = (
                    f"retired wave {record.get('retired_wave', '?')}"
                    f" · reason: {record.get('retirement_reason') or 'unknown'}"
                )
            else:
                lifecycle = (
                    f"active · admitted wave {record.get('admitted_wave', '?')}"
                    f" · ttl {record.get('ttl_waves', '?')} wave(s)"
                )
            lines.append(
                f"  {record.get('id', 'unknown')} · {lifecycle}"
                f" · proposal {_display_digest(record.get('proposal_digest', 'unknown'))}"
                f"{_dynamic_creation_reason(record)}"
            )
    acceptance = status.get("stateful_acceptance")
    if isinstance(acceptance, Mapping):
        case_id = str(acceptance.get("case_id", "unknown"))
        kind = str(acceptance.get("kind", "unknown"))
        result = str(acceptance.get("result", "unknown"))
        lines.extend(
            (
                "",
                "Stateful target acceptance",
                f"  Case: {case_id} · {kind}",
                f"  Result: {result}",
            )
        )
        expected = acceptance.get("expected")
        if isinstance(expected, Mapping):
            summary = json.dumps(expected, sort_keys=True, separators=(",", ":"))
            lines.append(f"  Expected: {summary}")
    waves = status.get("waves", ())
    if isinstance(waves, (tuple, list)):
        lines.extend(("", "Waves"))
        for wave in waves:
            if not isinstance(wave, Mapping):
                continue
            tasks = wave.get("tasks", ())
            task_values = tasks if isinstance(tasks, (tuple, list)) else ()
            lines.append(
                f"  {wave.get('wave', '?')}. {wave.get('kind', 'unknown')} · "
                f"{wave.get('status', 'unknown')} · {len(task_values)} task(s)"
            )
            for task in task_values:
                if isinstance(task, Mapping):
                    task_id = task.get("id", "unknown")
                    task_status = task.get("status", "unknown")
                    lines.append(f"     {task_id} · {task_status}")
    return "\n".join(lines)


def _render_saturation_choice_summary(campaign_id: str, status: Mapping[str, object]) -> str:
    """Keep the operator decision and every choice visible in a small terminal."""
    saturation = status.get("saturation")
    if not isinstance(saturation, Mapping):
        saturation = {}
    return "\n".join(
        (
            "Campaign needs an operator decision",
            f"{campaign_id} · {status.get('status', 'unknown')}",
            "Techniques: "
            f"{saturation.get('attempted_techniques', 0)}/"
            f"{saturation.get('admitted_techniques', 0)} attempted · "
            f"{saturation.get('untested_techniques', 0)} untested · "
            f"{saturation.get('catalogue_cells_unclear', 0)} inconclusive",
            "Exploratory hypotheses: "
            f"{saturation.get('hypothesis_cells_attempted', 0)}/"
            f"{saturation.get('hypothesis_cells_required', 0)} attempted",
            f"Full details: /campaign status {campaign_id}",
        )
    )


def _format_duration(value: object) -> str:
    """Render seconds as a compact duration, or 'n/a' when not yet estimable."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    total = max(0, int(value))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _display_digest(value: object, *, width: int = 8) -> str:
    """Keep terminal integrity identifiers readable without changing stored values."""
    text = str(value)
    prefix, separator, body = text.partition(":")
    if separator and len(body) > width:
        return f"{prefix}:{body[:width]}…"
    if not separator and len(text) > width * 2:
        return f"{text[:width]}…"
    return text


def _render_findings(findings: tuple[FindingView, ...]) -> str:
    if not findings:
        return "No findings recorded."
    rows = []
    for finding in findings:
        evidence = ", ".join(finding.evidence) or "none"
        rows.append(
            f"{finding.title} ({finding.id}) · {finding.severity} · {finding.disposition}\n"
            f"  {finding.summary}\n  Evidence: {evidence}"
        )
    return "\n\n".join(rows)


def _render_reports(reports: tuple[ReportView, ...]) -> str:
    if not reports:
        return "No reports generated."
    return "\n".join(
        f"{report.run} · {report.format} · {_report_display_status(report)} · "
        f"{report.finding_count} findings\n  Output: {report.output}"
        for report in reports
    )


def _render_doctor(checks: tuple[DiagnosticCheckView, ...]) -> str:
    if not checks:
        return "No diagnostics were returned. Check readiness integration."
    return "\n".join(
        f"{'✓' if check.status == 'ok' else '✕'} {check.name} · {check.status}\n  {check.detail}"
        for check in checks
    )


def _render_operational_rows(rows: tuple[OperationalEntryView, ...]) -> str:
    if not rows:
        return "No entries recorded."
    return "\n".join(
        f"{row.occurred_at or 'unknown time'} · {row.kind} · {row.summary}"
        + (f"\n  {row.detail}" if row.detail else "")
        for row in rows
    )


def _render_context_provenance(value: object) -> str:
    if not isinstance(value, Mapping):
        value = vars(value) if hasattr(value, "__dict__") else {}
    session_id = str(value.get("session_id") or "none")
    projection_head = value.get("projection_head")
    transcript_head = int(value.get("transcript_head") or 0)
    effective_head = transcript_head if projection_head is None else int(projection_head)
    raw_messages = value.get("messages", ())
    messages = raw_messages if isinstance(raw_messages, (list, tuple)) else ()
    message_rows = []
    for item in messages:
        if not isinstance(item, Mapping) and hasattr(item, "__dict__"):
            item = vars(item)
        if not isinstance(item, Mapping):
            continue
        message_id = str(item.get("message_id") or "unknown")
        source_id = str(item.get("source_message_id") or "unknown")
        inherited = "inherited" if source_id != message_id else "native"
        message_rows.append(
            f"  {int(item.get('sequence') or 0):>5} · {str(item.get('role') or 'unknown')}"
            f" · {inherited}\n"
            f"          message {message_id}\n          source  {source_id}"
        )
    raw_compactions = value.get("compactions", ())
    compactions = raw_compactions if isinstance(raw_compactions, (list, tuple)) else ()
    compaction_rows = []
    for item in compactions:
        if not isinstance(item, Mapping) and hasattr(item, "__dict__"):
            item = vars(item)
        if not isinstance(item, Mapping):
            continue
        compaction_rows.append(
            f"  {item.get('source_start')}-{item.get('source_end')} · "
            f"{item.get('algorithm')}\n"
            f"          source  {item.get('source_digest')}\n"
            f"          summary {item.get('summary_digest')}"
        )
    mode = "transcript head" if projection_head is None else "rewound projection"
    return (
        f"Session: {session_id}\nProjection: {mode} at sequence {effective_head}"
        f"\nDurable transcript head: {transcript_head}\n\n"
        "Message lineage (content intentionally omitted):\n"
        + ("\n".join(message_rows) if message_rows else "  no durable messages")
        + "\n\nCompaction records:\n"
        + ("\n".join(compaction_rows) if compaction_rows else "  none")
        + "\n\nActions: /branch SEQUENCE · /rewind SEQUENCE · /restore"
    )


def _render_permissions(
    mode: str,
    rules: tuple[PermissionRuleView, ...],
    history: tuple[PermissionAuditView, ...],
) -> str:
    rule_rows = [
        f"  {rule.tool} · {rule.effect} · {rule.decision}\n    Scope: {rule.scope}"
        for rule in rules
    ] or ["  none"]
    audit_rows = [
        f"  {audit.tool} · {audit.effect} · {audit.verdict} ({audit.mode})"
        f" · {audit.decision or 'default'}\n    Scope: {audit.scope or 'unknown'}"
        f"\n    {audit.reason}"
        for audit in history
    ] or ["  none"]
    return (
        f"Active mode: {mode}\n\nSession rules:\n"
        + "\n".join(rule_rows)
        + "\n\nHistory:\n"
        + "\n".join(audit_rows)
    )


def _render_promotions(
    candidates: tuple[PromotionCandidateView, ...],
    history: tuple[PromotionHistoryView, ...],
) -> str:
    candidate_rows = []
    for candidate in candidates:
        if candidate.graph_local_composition or not candidate.permanently_promotable:
            eligibility = "⛔ GRAPH-LOCAL — NOT PERMANENTLY PROMOTABLE"
        else:
            eligibility = "Eligible for operator-controlled promotion"
        gates = (
            "\n".join(
                f"    {'PASS' if gate.passed else 'FAIL'} · {gate.name} · {gate.detail}"
                for gate in candidate.gates
            )
            or "    Not evaluated"
        )
        failure = (
            f"\n  Evaluation failure: {candidate.evaluation_failure}"
            if candidate.evaluation_failure
            else ""
        )
        candidate_rows.append(
            f"{candidate.id} · {candidate.kind} v{candidate.version} · {candidate.status}\n"
            f"  {eligibility}\n  Proposed by: {candidate.proposed_by}\n"
            f"  Baseline: v{candidate.baseline_version} · {candidate.baseline_digest}\n"
            f"  Candidate digest: {candidate.content_digest}\n"
            f"  Exact diff:\n{candidate.exact_diff}\n  Evaluation gates:\n{gates}{failure}"
        )
    history_rows = [
        f"  {item.activated_at} · {item.action} · {item.candidate_id}"
        f" · operator {item.operator_id}"
        f" · previous {item.previous_candidate_id or 'none'}"
        for item in history
    ]
    return (
        "Candidates:\n"
        + ("\n\n".join(candidate_rows) if candidate_rows else "  none")
        + "\n\nActivation / rollback history:\n"
        + ("\n".join(history_rows) if history_rows else "  none")
        + "\n\nOperator actions: /promotion evaluate ID [SUITE] · "
        "/promotion accept ID REASON · /promotion reject ID REASON · "
        "/promotion certify ID EVALUATION EXPIRES_AT · /promotion activate ID · "
        "/promotion history [KIND] · /promotion rollback KIND"
    )


def _render_browser_contexts(contexts: tuple[BrowserContextView, ...]) -> str:
    if not contexts:
        return "No browser contexts recorded."
    rows = []
    for context in contexts:
        tabs = (
            "\n".join(
                f"    {tab.ref} · {tab.state} · {tab.url} · opener: {tab.opener or 'none'}"
                for tab in context.tabs
            )
            or "    none"
        )
        rows.append(
            f"{context.id} · {context.isolation} · {context.state}\n"
            f"  Target: {context.target} · Owner: {context.owner}"
            f" · Technique: {context.technique}\n"
            f"  Allowed origins: {', '.join(context.allowed_origins) or 'none'}\n"
            f"  Latest action: {context.latest_action or 'none'}\n"
            f"  Latest observation: {context.latest_observation or 'none'}\n"
            f"  Blockers: {', '.join(context.blockers) or 'none'}\n"
            f"  Evidence: {', '.join(context.evidence) or 'none'}\n"
            f"  Cleanup: {context.cleanup}\n  Tabs:\n{tabs}"
        )
    return "\n\n".join(rows)


def _render_memory(records: tuple[MemoryRecordView, ...]) -> str:
    if not records:
        return "No portable memory records found."
    rows = []
    for record in records:
        reviewer = record.reviewed_by or "pending review"
        conflicts = ", ".join(record.conflicts) or "none"
        turns = ", ".join(record.turn_uses) or "not used"
        rows.append(
            f"{record.record_id} · {record.kind} · {record.status}\n"
            f"  Bundle: {record.bundle_id}\n  Provenance: {record.source}\n"
            f"  Reviewed by: {reviewer}\n  Expires: {record.expires_at}\n"
            f"  Applicability: {record.applicability}\n  Conflicts: {conflicts}\n"
            f"  Digest: {record.digest}\n  Agent turns: {turns}"
        )
    return "\n\n".join(rows)


def _render_lessons(value: object) -> str:
    if isinstance(value, tuple) and all(isinstance(item, MemoryRecordView) for item in value):
        return _render_memory(value)
    if not isinstance(value, Mapping):
        return "No campaign lessons found."
    counts = value.get("counts", {})
    lessons = value.get("lessons", ())
    if not isinstance(counts, Mapping) or not isinstance(lessons, (tuple, list)):
        return "No campaign lessons found."
    header = " · ".join(
        f"{name.replace('_', ' ')}: {int(counts.get(name, 0))}"
        for name in ("finding", "negative_result", "successful_setup")
    )
    rows = []
    for lesson in lessons:
        if not isinstance(lesson, Mapping):
            continue
        rows.append(
            f"{lesson.get('outcome')} · {lesson.get('profile_id')} · wave {lesson.get('wave')}\n"
            f"  Campaign: {lesson.get('campaign_id')}\n  Task: {lesson.get('task_id')}\n"
            f"  Evidence: {_display_digest(lesson.get('evidence_digest'))}\n"
            f"  {lesson.get('content', '')}"
        )
    return header + ("\n\n" + "\n\n".join(rows) if rows else "\n\nNo campaign lessons found.")


def main() -> None:
    from .bootstrap import build_app

    build_app().run()


def _marked_provider_choices(
    choices: tuple[str, ...],
    provider: object,
    credentials: Mapping[str, str],
    installed: set[str],
) -> tuple[str, ...]:
    from .provider_catalog import PROVIDER_ROUTES

    provider = provider if isinstance(provider, Mapping) else {}
    endpoints = provider.get("model_endpoints", {})
    endpoints = endpoints if isinstance(endpoints, Mapping) else {}
    keyed_urls = {
        str(endpoint.get("base_url", "")).rstrip("/")
        for endpoint in endpoints.values()
        if isinstance(endpoint, Mapping) and str(endpoint.get("api_key", "")).strip()
    }
    if str(credentials.get("api_key", "")).strip():
        keyed_urls.add(str(provider.get("base_url", "")).rstrip("/"))
    known_urls = {route.base_url.rstrip("/") for route in PROVIDER_ROUTES if route.base_url}
    labels = []
    for index, choice in enumerate(choices):
        if index < len(PROVIDER_ROUTES):
            route = PROVIDER_ROUTES[index]
            if route.local:
                ready = ("ollama" if route.id == "ollama" else "lms") in installed
                suffix = "Installed" if ready else ""
            else:
                ready = (
                    route.base_url.rstrip("/") in keyed_urls
                    if route.base_url
                    else bool(keyed_urls - known_urls - {""})
                )
                suffix = "API key saved" if ready else ""
        else:
            executable = "codex" if choice.startswith("Codex") else "claude"
            suffix = "Installed" if executable in installed else ""
        labels.append(f"✓ {choice} · {suffix}" if suffix else choice)
    return tuple(labels)
