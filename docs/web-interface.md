# Merlin browser workspace

The optional browser workspace adapts pb2's actual React shell, UI primitives, typography,
resizable sidebar, and light/dark tokens to Merlin. It uses the same local-control daemon,
foreground conversation, durable campaigns, reports, and configuration as the TUI.
It does not embed the marketing mock or replace the campaign engine.

## Run from source

Prerequisites: Python 3.13+, uv, Node.js 22.18+ (or 24+), and pnpm.

```sh
uv sync --all-packages
pnpm --dir apps/merlin-web/frontend install --frozen-lockfile
pnpm --dir apps/merlin-web/frontend build
uv run merlin-web
```

The command opens a local, token-authenticated URL. Use `--no-open` to print it instead,
`--port 8766` to change the port, or `--socket-path PATH` for an isolated daemon.
The default address is `127.0.0.1`; this is a local operator console, not a remote multi-user service.
The startup URL grants local workspace access. Do not share it.

The build stages the compiled interface inside the Python package. To build its wheel:

```sh
uv build --package merlin-web --wheel
```

This feature is on `feature/web-interface`. Existing standalone CLI releases do not include
this web package yet. Use the source commands above until release packaging includes it.
Inside the source TUI, enter `/web` to open the current session directly. This starts a local
server inside the running TUI process, reuses its existing daemon connection and stream reader,
and opens the authenticated browser URL. Keep that terminal running. Repeating `/web` opens
the same workspace again. Port 8765 is preferred; if occupied, the embedded service selects an
available port without displacing the other service. Pending campaign review and current
conversation are retained, and ordinary browser messages steer ongoing recon. Tool approval
requests after opening `/web` are handled in the browser. Closing a browser tab does not stop
the TUI or campaign; exiting the TUI closes its embedded web service.

Only one foreground lease can own a daemon at a time. A separately launched `merlin-web`
process cannot attach alongside a TUI; use `/web` for shared access instead. Multiple browser
tabs share the same web workspace.

## TUI and browser workflow

| TUI surface | Browser surface |
| --- | --- |
| Target, scan effort, agent formation | New campaign form: Quickscan default, Recommended, Exhaustive, Exploration; Autonomous default or Opinionated |
| Recon and foreground conversation | Chat with expandable tool/reasoning activity; ordinary messages steer active recon without interrupting |
| Manifest review | Exact saved review, coverage/languages, models, judgments, limits, scope and digest-bound approval; hybrid offered only when admitted |
| Inline agent swarm | Active campaign status, wave, Lead activity, pause/resume and agent inspection |
| `/agents`, `/tasks` | Worker identities, assignments, retained transcripts, messages and task cancellation |
| `/campaigns`, campaign controls | Campaign list, status/evidence, guidance, pause, resume, cancel and saturation decisions |
| Coverage and exploration | Recorded technique outcomes, coverage chart, hypotheses, lineage, budget and pacing in campaign details |
| `/findings`, `/report` | Findings disposition/evidence; saved reports and HTML/Markdown/JSON/CSV exports |
| `/resume`, `/new` | Startup resume picker, saved-session search, new-session confirmation, restored prepared review |
| `/setup`, `/model` | Provider access, per-model endpoints, role models/transports/reasoning effort, JEV key and reconnect |
| `/targets`, `/library`, `/skills`, `/mcp` | Inventories, resource details, skill controls, MCP checks and remote MCP credential configuration |
| `/browser`, `/memory`, `/promotion` | Browser context takeover/cleanup, memory records, promotion candidates and decisions |
| `/mode`, `/permissions` | Current mode, saved permission rules, audit history and explicit pending tool decisions |
| `/doctor`, `/logs`, `/history`, `/context`, `/lessons` | Dedicated system views with retained backend data |
| Advanced slash commands | Command palette lists the actual TUI command catalogue; argument-bearing commands use the existing backend |

Some advanced operations use the command palette instead of a dedicated form (for example
promotion certification, report comparisons, memory review, support export, and branch/rewind).
The palette retains arguments; placeholders in the command reference must be replaced.
Browser view URLs preserve navigation and support Back/Forward. Cmd/Ctrl+K opens commands;
Cmd/Ctrl+N starts a new session. Dialogs support keyboard focus and Escape dismissal.

Stopping has separate scopes: interrupt ends the foreground turn; pause retains campaign state;
cancel stops remaining campaign work and requests durable cleanup. Campaign guidance is queued
for the Lead's decision boundary. A queued message is not proof it has been acted on.

## Security and data handling

Browser requests pass through a local authenticated bridge and the existing admitted operator
methods. Origin/Host checks reject cross-origin control and DNS rebinding. The token becomes an
HttpOnly, SameSite=Strict cookie. Provider keys use the existing protected configuration store;
the setup status response exposes key presence, never saved key values. Export downloads only
serve artifacts registered by the report backend. HTML evidence is sandboxed and cannot run
scripts against the operator console. Live transcript replay is bounded; older evidence remains
in durable history.

## Source attribution

The reused shell, styles and primitives come from the Apache-2.0 pb2 repository at commit
`1ed78947`. Mermaid/Pirateboys product state and Electron integration were replaced with a
Merlin browser adapter. First-party browser orchestration and build tooling are TypeScript;
the bridge is Python to reuse Merlin's existing local-control client directly.

## Verification

Run the HTTP and shared transport regressions:

```sh
uv run pytest apps/merlin-web/tests/test_workspace.py apps/merlin-tui/tests/test_local_control.py apps/merlin-tui/tests/test_foreground_steering_transport.py apps/merlin-tui/tests/test_foreground_operator_messaging.py
uv run ruff check apps/merlin-web
pnpm --dir apps/merlin-web/frontend build
```

`tests/browser_fixture.py` is explicitly isolated acceptance data: it makes no provider or
live target requests. It is not part of the installed app and does not establish attack
quality. Browser acceptance and real isolated daemon startup are recorded separately.

### Acceptance evidence — 2026-10-09

- 89 web bridge and shared TUI transport regression tests passed; Ruff and TypeScript checks passed.
- Production frontend build and Python wheel build passed. The installed wheel contains the server, compiled frontend, and Merlin logo.
- An isolated real daemon and embedded PostgreSQL started successfully, using temporary configuration and sockets. No live provider or target requests were made.
- Chrome fixture acceptance covered automatic resume, restored prepared review, approval, wave status, agent transcript inspection, coverage/discovery details, completion summary, exports, and navigation across the resource/system views. Back/Forward and keyboard dialog dismissal worked.
- Eight viewport sizes were checked for document overflow (390–1440 pixels); no document-level overflow or captured console errors occurred in those checks. This is not a complete accessibility audit or an interaction test of every resource action.
- Export bytes and attachment headers were verified through HTTP tests. The browser export action retained the workspace; a browser-saved download file was not independently verified.
- Existing campaign engine code was not modified. The TUI gained optional `/web` integration. GitNexus reported low risk for indexed tracked changes; the new web package is not yet represented in its symbol graph.

Screenshots in `/private/tmp/merlin-web-evidence/` show explicitly labeled acceptance fixtures. They do not claim live scan results. The feature remains source-only until integrated into release distribution and CI.

### `/web` verification

Four mounted-TUI HTTP tests verify current history, repeated server reuse, no second lease,
recon steering, browser chat through the existing TUI stream reader, prepared-review retention
on keyboard submission, permission ownership and server cleanup without closing the backend.
The web/shared transport suite passes 93 tests; all 320 mounted TUI tests also pass,
including all 99 keyboard command-matrix cases and the dedicated `/web` tests.
Both updated Python wheels and the production TypeScript frontend build successfully.
GitNexus marks the combined command/stream change high risk; those paths received the mounted
regressions. New web-package symbols are not yet indexed.

Start the checkout TUI with `./merlin`, then enter `/web` (`./merlin` wraps `uv run merlin`). Chrome acceptance verified that
the existing fixture conversation appears without a resume picker and that browser chat
receives a response through the current TUI stream reader. No paid provider or live target
was involved. Previously published standalone release binaries do not yet contain this feature; the checkout’s `./merlin` uses the updated source.


### Loading and navigation repair — 2026-10-09

The original fixture acceptance did not prove that populated real-daemon records rendered.
The daemon client decodes records into `SimpleNamespace`, but the bridge reused a protocol
encoder that stringified those records. This dropped table rows and pagination silently.
The browser bridge now reverses that decoding recursively before returning JSON; shared
TUI serialization and the campaign engine are unchanged.

The sidebar now has six primary destinations: Chat, Campaigns, Results, Resources, Sessions,
and Settings. Secondary navigation exposes agents/tasks/coverage under Campaigns,
findings/reports under Results, resource pages under Resources, history/context/lessons
under Sessions, and provider/model/permission/diagnostic pages under Settings.

Page data loads independently of unrelated inventories. HTTP requests have bounded waits;
foreground model execution does not. Chat shows pending acceptance, preserves drafts on
failure, and reuses request identities for uncertain retries. A bridge submission lock also
prevents concurrent retries from delivering the same guidance twice. Connection loss is
visible and recovery clears the connection warning. Failed actions preserve their dialogs
and inputs, and their errors appear inside the active dialog. New-session state clears only
when the daemon acknowledges the change.

Verified after the repair:

- All 20 operator page data APIs returned HTTP 200 through an isolated real daemon and
  embedded PostgreSQL. Runtime/context/lessons and populated saved-session/history records
  now retain their structured shapes.
- Chat completed through a local OpenAI-compatible streaming provider and the real daemon.
  This checks transport, not external provider availability or attack quality.
- The same chat passed through a mounted TUI and `/web`: the existing foreground lease was
  reused, content appeared in both interfaces, and saved-session pagination remained structured.
- 43 rendered component tests cover all 22 data views, the main/secondary navigation,
  slow inventories, chat acceptance and content, duplicate submission, error recovery,
  record-specific action routing, failed cancellation/guidance, and campaign preparation
  target/profile/formation selections.
- 42 Python web tests cover authentication, scope/approval identity, exports, current-TUI
  bridging, populated nested records, failed new-session preservation and concurrent retry
  deduplication. The additional shared TUI transport regression suite has 70 passing tests.
- TypeScript production build, Python lint, formatting, canonical `./merlin --version`, and
  wheel builds pass.

**Audit verdict: incomplete.** Chrome extension control disconnected during verification;
native Chrome control was subsequently denied. The repaired layout therefore has not had
its new browser visual/responsive/contrast audit, browser download verification, or every
resource mutation tested against populated real services. Component routing assertions do
not establish that those external services are available or healthy. Previous fixture
screenshots show the earlier layout and must not be presented as proof of the repaired UI.

Run the maintained frontend regressions with:

```sh
pnpm --dir apps/merlin-web/frontend test
```

### Resume, campaign inventory, and provider repair — 2026-10-10

- Campaign inventory now queries the current session directly instead of loading every saved campaign manifest and discarding unrelated sessions. An explicit unlimited query preserves sessions with more than 50 campaigns. The web Campaigns page no longer issues its inventory request twice per refresh.
- Sessions uses the paginated saved-session API. Resume preserves the current conversation until the daemon acknowledges the switch, closes both selection and confirmation dialogs, and reloads the restored conversation. The confirmation identifies the title, timestamp, and exact session ID. Failed submissions retain the dialog and show the error.
- Provider setup supports all eight existing provider choices and additional per-model endpoint/key pairs. Saved routes show endpoint and key-presence status without returning key values. Blank edits preserve the key only for the same destination; changing a destination does not carry its old key across.
- Real isolated PostgreSQL/daemon + mounted TUI/web test: created two saved conversations using a local HTTP provider fixture; resumed the exact earlier session ID; observed the acknowledged reset and successful campaign inventory. Resume completed in approximately 0.3 seconds. No external model or target calls were made.
- Component coverage includes selected-session submission, failed and pending resume, restored conversation text, and additional provider credentials. These checks do not certify every web route or a full visual/accessibility audit. Native browser automation could render the isolated picker but subsequent interactions failed with `noWindowsAvailable`; in-app browser was unavailable.

Restart Merlin with `./merlin`, then enter `/web` to load changed Python handlers. Refreshing the browser only updates the built frontend; it cannot replace already imported daemon/TUI code.

### Local workspace connection and restart repair — 2026-10-10

- Browser cookies now have a unique name for each workspace. Cookies are scoped to hosts, not ports; the previous shared name let a second localhost workspace overwrite the first workspace's authentication. A regression uses two actual HTTP servers and a shared browser cookie jar.
- A failed or expired browser connection shows a connection screen rather than a usable-looking empty conversation. Opening a fresh `/web` token in an existing tab triggers authentication again without requiring a full reload.
- Chat content uses bounded child widths and fixed outer padding. Chrome fixture inspection confirmed all three welcome cards fit in the content area. Campaign inventory, the selected-session confirmation, and successful resume back into the saved review were also exercised through the browser.
- `./merlin restart` now reports foreground ownership conflicts without a Python traceback. It does not forcibly terminate another terminal's work. Exit the owning Merlin terminal first, then run `./merlin restart`, launch `./merlin`, and enter `/web`. Merely restarting the daemon does not replace the web server embedded in an already-open TUI process.
- These targeted checks do not certify every resource operation or all responsive viewports. The browser checks use an isolated fixture, not a live security target.

### Master approval integration — 2026-10-10

`feature/web-interface` includes local `master` through `0abe30e`. The existing web work was preserved during the fast-forward integration.

Browser permission cards now show the same redacted execution details as the TUI, including the shell command and resolved working directory. For broker-eligible shell requests, they offer **Allow this command + directory (session)** alongside Allow once and Deny. This decision passes through the existing daemon permission engine; the browser does not create a broader shell allowance. Changed arguments or a changed directory require a new approval. Ineligible tools and expired requests cannot obtain this allowance through the HTTP endpoint.

Browser permission snapshots retain reviewed display text and eligibility metadata rather than raw invocation credentials. Regression coverage includes redaction in event replay, eligibility enforcement, the exact browser decision payload, and HTTP-to-broker reuse/reapproval boundaries.

Verification after integration: 567 Python regressions and 54 frontend component tests passed; TypeScript/Vite build, Ruff, and diff whitespace checks passed. Chrome fixture verification confirmed readable command/directory details, successful submission of the exact-command session decision, and removal of the resolved permission card. This targeted check does not constitute a full web UI audit.

### Resume dialog and Chat inventory repair — 2026-10-10

Closing a recent-session confirmation now clears the selected record and its details. Previously, closing the confirmation immediately opened an empty generic details dialog. Component regressions cover both Close and Escape, followed by an intentional reopen and resume. Chrome fixture verification confirmed Settings remains visible without a ghost dialog after dismissal.

Chat and Coverage request reports for the current session instead of enumerating every historical campaign. The Reports page retains daemon-wide history, makes one report request per refresh, and exposes failures through its retry control. Auxiliary inventory failures no longer become Chat page errors. A new backend regression asserts that session-scoped report lookup never enumerates historical campaigns, including when the session has no campaign.

Verification: 58 frontend tests and 124 backend/web tests passed. TypeScript/Vite build, Ruff, and diff whitespace checks passed. These are targeted regressions and an isolated browser check; the user's live session was not restarted or exercised. The backend change requires restarting the owning Merlin process and reopening `/web`.

### Foreground streaming feedback — 2026-10-10

Incoming reasoning opens while the foreground turn is active, appends chunks to the same disclosure, and collapses on completion. The operator can reopen completed reasoning. The reasoning pane follows new text unless the operator scrolls up. Tool disclosures show the received activity text in their summary. An inline spinner and elapsed-time indicator distinguish sending, waiting for the model, thinking, writing a response, tool activity, and approval waits. Reduced-motion preferences disable spinner rotation. Actual reasoning or tool events clear the stale message-accepted banner.

Verification: 60 frontend tests passed, including incremental reasoning, tool activity, completion collapse, and a busy model that emits no reasoning. TypeScript/Vite build passed and the compiled package assets were updated. Chrome fixture inspection confirmed visible reasoning and the inline activity indicator in the desktop layout. This is a targeted streaming check, not certification of all web routes or the live target campaign.

### Campaign choice cards and Markdown — 2026-10-10

Known campaign profile and agent-composition prompts now offer native radio-card groups with descriptions, Quickscan and Autonomous defaults, selected/focus states, and a single Continue action. Detection uses the joined response chunks from the latest operator turn, rather than one exact sentence in the final token. Continue sends explicit option names through the existing conversation path; it does not approve execution. Historical prompts do not offer controls after a newer operator reply. Arbitrary model questions remain text; this is not a generic structured-question protocol.

Assistant messages render through react-markdown and remark-gfm with raw HTML disabled. Regressions cover the screenshot wording split across stream chunks, default selections, changing both choices, one submission with explicit names, stale-choice removal, and unsafe HTML/link handling. All 63 frontend tests and the TypeScript/Vite build passed. Chrome fixture interaction verified both selected radio values and the outgoing named-choice message followed by the campaign review. The fixture returns a fixed review; this check does not establish live model adherence to selected options.

Conversation alignment repair: thread children now fill the same bounded column instead of centering according to each block's intrinsic text width. User messages, assistant messages, reasoning, and activity share horizontal padding. Explicit Markdown list styles restore numbering and bullets removed by the base CSS reset. The rebuilt desktop fixture was visually checked with a user bubble, short reasoning, usage, and a longer numbered assistant response; their text starts align. This change only affects CSS.

Response usage and tool wrapping: token counts now render as muted, right-aligned response footers instead of separate transcript rows. Usage arriving before final answer text transfers from reasoning to the answer. Long tool summaries are abbreviated; expanded payloads, code blocks, and unbroken references wrap within their panels. All 67 frontend tests and the TypeScript/Vite build passed. An isolated Chrome fixture visually confirmed the response footer and an expanded browser snapshot containing a long unbroken reference without horizontal page overflow. Compiled assets were updated; no live target calls were made.

### Shared structured operator questions — 2026-10-10

The foreground now exposes `request_user_input` with shared validated contracts in `merlin_contracts.user_input`. Questions carry stable IDs, labels, descriptions, defaults, single/multiple selection, and optional free text. Every question with choices receives a default selection: an explicit default, otherwise the first option. Recognized campaign profile and composition choices always use Quickscan and Autonomous regardless of model option order. The shared contract supplies these defaults to both web and TUI; free-text-only questions remain blank. Defaults never submit automatically. The model identity now directs all choice and missing-input questions through the tool rather than numbered prose. The tool is excluded from campaign-worker projections. Provider use remains model-driven; existing prose-only history is not retroactively interpreted as structured questions.

A successful question saves a normal terminal tool receipt with `awaiting_input` and ends the foreground turn. Later calls in the same batch are explicitly deferred with terminal receipts. This avoids a pending network request or human-input tool timeout. The selected IDs and context return in a validated `MERLIN_USER_INPUT_RESPONSE` user message; the original request remains in the journal. This is a resumable foreground turn, not an unresolved provider tool call. Backend validation rejects stale requests, unknown/duplicate options, invalid cardinality, incomplete answers, and structured answers submitted through busy-turn steering. Free-form chat can answer, correct, or supersede a pending question.

Web renders generic radio/checkbox cards and an optional text field. Authoritative snapshots restore pending questions across browser refresh and event-buffer expiry. Answered controls are read-only, and exact selections remain visible after refresh. TUI uses the same data in a keyboard-accessible modal, returns the same response envelope, and dismisses the matching modal when another client answers. `/resume` reconstructs pending questions from the conversation journal. Defaults do not submit automatically; choices grant no execution approval or additional scope. Secrets belong in secure setup or browser takeover.

Verification: 432 targeted Python contract/kernel/backend/transport/web tests plus 7 TUI streaming/approval regressions passed; 72 frontend tests passed. TypeScript/Vite build and Ruff passed; compiled assets were staged. Chrome acceptance used the real question tool, ToolBroker, Kernel and KernelBackend with a deterministic provider and a retained in-memory journal. Recreating the kernel/backend restored the pending question; browser refresh retained it; selecting the saved-session option reached the provider with the exact ID; answered controls and selection survived another refresh. No live model or target calls were made. A broader run also found a context-compaction assertion that reproduces with the HEAD identity, and an independent durable-swarm finding-export fixture failure (no low-severity draft); these remain unresolved outside the structured-question flow.

These backend changes require restarting the owning Merlin daemon/TUI, reopening `/web`, and resuming the intended session. Refreshing a page alone cannot register the new foreground tool in an already running daemon.

### Resource-page repair (2026-10-10)

The saved provider list now includes the default provider alongside per-model
endpoints. Exact model/endpoint duplicates are combined; other endpoints remain
separate. `setup_status` returns only a boolean for default API-key presence,
never the key. Successful default-provider saves reload the saved list.

Diagnostics unwraps the backend `(healthy, checks)` response. Lessons unwraps
`{counts, lessons}`. Context separates session metadata from messages and
compactions. Resource inventories use meaningful titles, page-specific help,
visible operational fields, and labeled expandable details instead of a raw JSON
viewer. Missing statuses do not acquire a synthetic “recorded” badge.

Lists render 25 loaded records at a time. Search covers the loaded inventory,
including records beyond the visible batch; “Show 25 more” reveals the next
batch. This is browser rendering pagination, not storage pagination. Logs and
History still expose the backend's latest 100 records; that limit is displayed.
Long field text and nested collections have bounded previews with expansion.

Verification: 79 frontend tests; 49 web API tests; TypeScript/Vite production
build, Ruff, and whitespace checks. Added tests cover default credential
presence without secret disclosure, provider deduplication, actual response
shapes, detail expansion, diagnostic refresh, and searching beyond the rendered
log batch. A Chrome acceptance fixture exercised 14 repaired routes at eight
viewport sizes (1440×1000, 768×1024, 1024×768, 390×844, 844×390, and widths 1279,
1023, 767). Every sampled route had one h1 and no document horizontal overflow.
Browser interactions verified the provider list, diagnostic rows, log load-more
and filtering, resource navigation, and lesson detail dismissal. No captured
console errors. The fixture made no provider or target requests and did not
change operator settings.

Full UI certification remains incomplete: no complete accessibility/contrast
scan, no exhaustive slow/error/empty-state matrix, and no live mutation audit of
promotion activation, browser takeover/cleanup, MCP restart, or permission-mode
changes. Responsive overflow checks do not prove every control's usability.

### Restart recovery waits for resume

Daemon startup pauses persisted executing campaigns and running worker schedules before
reopening them. It does not restart reconnaissance or browser target work while the
startup session picker is open. Starting a new session leaves saved work paused.
Resuming a saved session resumes only that session's paused campaigns; individual
campaign resume remains available. Checkpoints and attempt history are retained, and
cancelled schedules can still finish their owned cleanup obligations. The watchdog
continues to repair supervisors for work explicitly started or resumed in the current
daemon.

### Streaming scroll and manual sign-in

Chat and expanded thinking follow rendered content growth while the reader remains
at the bottom. Scrolling up pauses following until the reader returns to the bottom;
delayed layout changes and disclosure expansion also update the scroll position.

Browser takeover permits manual HTTP/HTTPS navigation to external sign-in providers.
Automation cannot acquire or transfer an operator-controlled context, and manual
sign-in responses are excluded from reconnaissance network capture. Explicit browser
resume restores automation only after every open page is back within the target's
existing navigation bounds. Target allowed origins remain unchanged.

Validation: 183 browser regression tests, including a real Chromium two-origin
login fixture; 85 frontend tests and 53 web server tests; TypeScript and production asset build. The login
fixture exercises blocked automation, manual external navigation, refusal to hand
back an external page, and restored bounds after return. It does not sign in to a
real Google account.

A targeted Chrome streaming fixture confirmed both chat and thinking follow the
bottom, scrollback stays fixed while new output arrives, and returning to the
bottom resumes following. The fixture also exercises chat mounting after loading.

### Browser hand-back continues within the foreground turn

Chat-directed browser resume uses `operator_runtime` with
`operation=resume_browser_context`. The browser service completes its existing
ownership and navigation-bound checks before the tool returns. The model receives
that result in the current turn and can verify authentication and continue recon.
An attempted `/browser resume` through the queued `operator_command` tool returns
an instruction to use the direct operation; terminal commands remain available.
This avoids promising follow-up work from a command dispatched after the model
turn has already ended. A failed resume is returned as an error in that same turn.

An isolated real-kernel/tool-broker fixture verifies queued-path recovery,
completed hand-back, and a subsequent browser observation with one user message;
its failure variant verifies no observation follows a rejected hand-back. The
provider and browser responses are scripted; no live target or account was used.
All 151 operator-control, backend, and web-server regression tests passed.

## Chat scrollback and campaign review

Chat initially reads the newest 50 persisted messages. Scrolling near the top loads
50 older messages at a time with a stable sequence cursor, a loading indicator, and
retry on failure. Prepending preserves the visible message; new sessions discard
in-flight pages from the previous session. Reads respect the active rewind projection.
An active foreground turn uses live events after the saved transcript, without replaying
the full in-memory event buffer during initial load.

The review presents objective, target, profile, coverage, estimated spend, and explicit
budget limits first. Empty budgets mean no configured limits. Estimates are not caps.
Detailed sections start collapsed; collections have filtering and 25-entry pages.
Technique names, language, and mutation variant are separated from full identifiers.
“New” cross-campaign cells mean no matching history record, not new findings.

Approval and cancellation show immediate progress and reject repeat clicks while
waiting. Approval still waits for the backend to validate the exact persisted digest
and durably prepare the execution schedule; the browser no longer waits for a full
inventory refresh before showing success. Request changes fills the message composer;
hybrid requests a new reviewed plan rather than executing the existing one.

Language evidence affects ordering, not admission. Previously prepared manifests can
still contain legacy language deferrals and must be prepared again to include those
cells; this change does not silently modify an already reviewed digest. The displayed
baseline is the adapter's recorded default language, or unknown when absent.

Verification for these changes includes real PostgreSQL paging across inserts and
rewinds, active-session boundary checks, browser hook/component regressions, and an
isolated Chrome fixture with 123 messages and a 10,212-entry legacy review. These
fixtures do not execute a scan against a live target.

### Reloading saved chat

F5 requests the newest persisted history page once, then starts live polling from
the snapshot cursor. Earlier messages load automatically when scrolling near the
top, with the existing reading position preserved. Saved history is not replayed
through the 650 ms live polling timer.

For an older running server missing `/api/history`, the compatibility loader drains
its retained event batches before rendering, merges response chunks, and opens on
the newest 50 entries. Earlier retained entries remain available through scroll-up
paging. Access to archive entries beyond that old server's retained buffer requires
restarting Merlin and reopening `/web`. Authentication and storage errors never
trigger this fallback.

Verified in isolated Chrome fixtures: F5 displays messages 74–123 immediately as
one page; scrolling up prepends 24–73 without jumping. A missing-route fixture with
1,234 events opens on 1,185–1,234 and loads 1,135–1,184 on upward scroll. Regression
tests additionally check every retained older page, response chunks spanning event
batches, active-turn separation, and a non-advancing cursor.

A capped report now reads “Scan finished · limit reached.” Its separate coverage
assessment remains visible; reaching a configured limit does not mean exhaustive
coverage or a fully resolved assessment.
