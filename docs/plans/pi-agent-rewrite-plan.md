# Merlin Pi rewrite: phases and release checkpoints

Status: implementation plan  
Date: 2026-10-31  
Architecture basis: `docs/architecture/merlin-revamp.md`; this plan supersedes its old-Merlin benchmark and migration gates

## Outcome and constraints

Rewrite Merlin as `merlin-next` on a dedicated branch, using Pi's agent loop, providers, sessions,
steering, and TUI substrate. One foreground attacker owns live-target writes. Browser and direct
HTTP/API are peer transports; specialists and dynamic workflows are optional and advisory.

The current Merlin release is the stable reference, not a migration dependency. The rewrite must not
replace, upgrade, stop, reconfigure, or delete it. Both versions must install, start, run, restart,
upgrade, and uninstall independently on the same Mac.

This is a gated full rewrite, not a big-bang replacement. Each checkpoint must leave the stable
release untouched and the new branch runnable or explicitly non-releasable.

## Checkpoint 0 — branch, identity, and coexistence contract

Create a dedicated branch such as `revamp/pi-agent`. Keep unrelated working-tree changes out of the
rewrite. Pin the exact Pi upstream revision and record its license and dependency notices.

Define the side-by-side contract before implementation:

| Resource | Stable Merlin | Pi rewrite |
|---|---|---|
| Command | `merlin` | `merlin-next` |
| macOS app / bundle ID | existing values | distinct `merlin-next` values |
| Configuration | `~/.merlin-python/` | `~/.merlin-next/` |
| Data and run records | existing locations | `~/.merlin-next/` or a distinct application-support directory |
| Logs, caches, temporary files | existing locations | separate next-specific locations |
| Daemon socket / lock / PID | existing names | distinct next-specific names |
| Embedded PostgreSQL | existing cluster/port | no shared cluster; own storage and dynamically allocated or distinct port |
| Browser profiles | existing profiles | separate default profiles; no implicit reuse |
| Python / Node environments | existing release runtime | separate packaged/runtime environment |
| Global npm / uv entry points | unchanged | no replacement of `merlin` or Pi's own `pi` command |

The rewrite may import stable data only through an explicit, read-only export/import command. Never
open the stable database for writes or silently migrate its configuration, credentials, sessions, or
browser profiles.

**Exit gate**

- A clean install of the rewrite does not alter the stable command, files, daemon, database, or browser profile.
- `merlin --version` and `merlin-next --version` report distinct product/build identities.
- Both TUIs can start after either installation order and in either launch order.
- Starting, stopping, restarting, or crashing one daemon does not affect the other.
- Concurrent smoke runs use separate state and produce separate logs.
- Uninstalling the rewrite leaves the stable release fully functional, and vice versa.
- An automated macOS coexistence acceptance script covers these cases and runs against packaged builds, not only source checkouts.

**Rollback checkpoint:** remove only the next-specific installation and branch artifacts. Stable Merlin requires no restoration.

## Checkpoint 1 — thin Pi runtime skeleton

Build the smallest executable TypeScript application around Pi and reuse its existing capabilities:

- agent/tool loop, streaming, steering, pause, and stop;
- provider authentication, provider/model selection, and model configuration;
- session persistence, resume, forking, compaction, and transcript history;
- MCP, custom tools, and extensions;
- TUI components and session interaction.

Merlin Next supplies only the red-team layer: one scoped objective and primary attacker, append-only
run events with secret redaction, lightweight target/effect state, independent verification, reporting,
and external call/time/token/cost/concurrency limits. Keep Pi provider configuration isolated under the
next-specific configuration root without replacing the user's existing Pi or stable Merlin setup.

Do not import campaigns, waves, portfolios, mandatory Lead supervision, ARM ranking, distributed
scheduling, or PostgreSQL. Do not build a persistent runner for live-process reconnection. Closing the
terminal may stop active execution; reopening the TUI resumes the saved Pi session, reloads the run
summary and evidence, and re-observes the target before any new effect.

Maintain three distinct records:

- **Pi transcript:** complete conversation and tool history, used for resume and same-objective forks.
- **Run summary:** compact objective, status, target state, discoveries, effective/ineffective/uncertain
  attempts, findings, related sessions, next hypotheses, and budget usage. Update after meaningful
  discoveries and at completion; use it to recall old runs without loading full transcripts.
- **Evidence record:** authoritative redacted effects, observations, scores, artifacts, and verification.

Fork sessions for related experiments on the same objective. Start a fresh session with a run summary
for a new target or old historical lesson. Reports never depend on transcript context surviving.

**Exit gate**

- A deterministic mock provider and target complete an end-to-end run without any campaign machinery.
- Existing Pi provider/auth and MCP integration work without a parallel provider or MCP manager.
- Steering reaches the active agent; pause/stop prevent subsequent target effects.
- Resume restores conversation, run summary, evidence, and budget state without replaying target mutations.
- The first action after resume is target re-observation; recorded historical state is not treated as live.
- An agent can list/read run summaries and selectively open a related transcript or evidence record.
- A same-objective fork inherits Pi context; a fresh run can use only a compact summary.
- Unit and integration tests cover budgets, cancellation, resume, recall, and secret redaction.
- The stable-release coexistence acceptance still passes.

**Rollback checkpoint:** the skeleton is self-contained; delete its next-specific runtime and records.

## Checkpoint 2 — adaptive discovery, observable TUI, and browser execution

Implement the operational TUI around current target state, hypothesis, action, transport, evidence,
budget, recovery, and steering. Show concise decision records, not hidden chain-of-thought or scheduler
activity.

Add browser observation, screenshots/vision, input, navigation, and independent target-state verification.
Enforce one writer per browser/shared mutable session.

Discovery is a continuous agent-led loop, not a mandatory reconnaissance phase:

`Observe → Discover → Hypothesize → Test → Measure → Share → Adapt`

The agent may test a promising hypothesis before completing a checklist, switch immediately between
browser and API, and return to discovery whenever it finds a new endpoint, level, behavior, or attack
surface. It chooses techniques and mutations from observed results; no predefined portfolio or wave
controls that choice.

Track lightweight observed target state, including current level, score, authenticated session, known
endpoints, tested techniques, useful results, verified findings, active transport contexts, and budgets.
This is target evidence, not campaign bureaucracy. The agent interprets it and decides the next action.

A roadblock triggers re-observation and adaptation rather than an identical retry or immediate terminal
failure. After a modal, navigation, stale context, rejection, authorization failure, timeout, or unknown
effect, the agent must establish what changed and whether the effect occurred before continuing. It may
then continue, mutate the technique, switch transport, refresh authentication, or discover again.

**Exit gate**

- Complete an owned or authorized interactive target journey from objective to independently verified result.
- The agent can move discovery → browser test → API test → discovery without a phase transition command.
- A useful observation produces a new technique or mutation without a predefined attack portfolio.
- Modal, navigation, level, stale-tab, rejection, timeout, and unknown-effect fixtures force re-observation.
- Acceptance tests prove an uncertain effect is not blindly repeated.
- A wrong-target or wrong-context action is rejected before dispatch.
- The primary screen answers what is happening, why, on which target, and what proves the result.
- Keyboard steering, pause, resume, stop, and reconnect behavior pass terminal acceptance tests.
- No ordinary action waits for a Lead, specialist, wave, formal reconnaissance checklist, or review merge.

**Rollback checkpoint:** disable the browser feature set; the runtime skeleton and stable release remain usable.

## Checkpoint 3 — direct HTTP/API transport, reporting, and optional proxy

Add capture and promotion of browser/network exchanges into replayable endpoint descriptors. Store
secret references rather than credentials in logs. Validate method, URL, request template, mutable
fields, response parser, target/session binding, and success oracle before replay.

Direct captured HTTP must work without a proxy. Add an optional proxy adapter for automatic browser
capture, hidden endpoint/parameter discovery, request replay and mutation, authentication observation,
and routing captured traffic to API workers. The product must remain fully functional when no proxy is
configured.

Reporting is a primary product output, not a post-run summary of model prose. Continuously record the
target and scope, hypothesis, technique, redacted request/payload, response, screenshots where useful,
score/state changes, verified outcome, reproduction steps, failures, near misses, uncertainty, latency,
tokens, and cost. Generate intermediate and final reports from this evidence. The attacker cannot mark
its own claim verified.

**Exit gate**

- A browser-discovered endpoint executes a valid direct API attempt without browser re-entry.
- A proxy-captured exchange can become the same replayable endpoint descriptor when proxy support is enabled.
- Redaction tests prove credentials and tokens do not enter logs, reports, proxy output, or the TUI.
- Unknown effects are reconciled by observation and are not blindly repeated.
- Browser, direct API, and proxy results feed the same independent verification and evidence timeline.
- An operator can open an evidence-backed intermediate report while the run is active.
- A final report contains reproducible successful inputs, observable results, target identity, timestamps,
  failures/near misses, and explicit verification status.
- Contract tests cover malformed descriptors, rotated authentication, changed target state, and ambiguous responses.

**Rollback checkpoint:** disable API replay and proxy support independently while retaining browser operation
and reporting.

## Checkpoint 4 — adaptive parallel browsers, API workers, and specialists

Add dynamic API fan-out for independent techniques and payload sets, subject to aggregate rate, target-call,
cost, and concurrency limits. Add parallel browser workers with isolated browser profiles, target sessions,
and writable contexts. Never share one tab or mutable authenticated context between writers.

Workers share observations, target state, and verified evidence rather than Lead orders. A promising result
may dynamically create related mutations across browser or API workers. Start with one worker by default;
fan out only when independent techniques or sessions justify it. Shared mutable state remains serialized.

Add optional snapshot-based vision, payload, response-analysis, and verification specialists. They return
advice to the foreground attacker and never mutate the live target. Dynamic workflow proposals must pass
deterministic partition and fan-out checks.

**Exit gate**

- Two browser workers test different techniques concurrently with separate profiles and target sessions.
- Isolated API workers test different techniques/payloads concurrently and beat serial wall time.
- A discovered browser technique can produce adaptive API mutations, and vice versa.
- Workers see shared evidence without acquiring write access to another worker's live context.
- Shared-state contamination tests show serialization when isolation is absent.
- The normal single-target path completes with zero specialists and no mandatory swarm.
- Specialist or worker failure, timeout, or cancellation does not strand the other workers.
- Budgets bound total target calls and aggregate worker cost, not merely per-worker usage.

**Rollback checkpoint:** set fan-out to one and disable specialists without losing core functionality.

## Checkpoint 5 — feature inventory and full rewrite parity

Create an explicit disposition for every current Merlin capability: `reimplemented`, `intentionally
removed`, `deferred`, or `import-only`. Do not preserve a feature merely because the old release has it.

Expected reimplementation includes operator setup, provider routing, sessions, target tools, evidence,
reports, pack/fixture loading, and authorized browser/API assessments. Cross-process recovery, stable
multi-target identity, distributed workers, portfolios, and scoring require measured promotion triggers.

Provide versioned, read-only importers only for data users explicitly choose to carry forward. Keep
the stable format unchanged and make imports repeatable without duplicating records.

**Exit gate**

- Every current user-facing command and workflow has a documented disposition and owner.
- All capabilities marked `reimplemented` have end-to-end acceptance coverage in the rewrite.
- Removed capabilities have product rationale and do not leave dead commands or broken imports.
- Import tests prove stable data is unchanged and repeated imports are idempotent.
- No next runtime component connects to stable Merlin's daemon, socket, database, or internal APIs.

**Rollback checkpoint:** continue shipping stable Merlin; next remains opt-in and no migration is forced.

## Checkpoint 6 — release candidate

Exercise `merlin-next` against representative authorized targets, including interactive browser levels,
replayable APIs, false-positive traps, timeouts, and restart boundaries. This is acceptance testing of
the new product, not a benchmark against old Merlin. Record results and known limitations.

Measure time to first target action, time to verified success, valid attempts per minute, target actions
per model call, API speedup, false-positive rate, duplicate effects, recovery time, tokens, and cost.
Use these measurements to find defects and unnecessary overhead, not to justify migration through comparison.

**Exit gate**

- Every required workflow completes successfully with independent verification.
- Target binding, duplicate-effect handling, budgets, cancellation, and recovery pass their acceptance tests.
- The full rewrite test suite, packaged acceptance suite, and macOS coexistence suite pass.
- A clean-machine install has no source-checkout, Homebrew, global npm, or stable-Merlin dependency.
- Upgrade, downgrade, uninstall, and reinstall tests preserve the isolation contract.
- License/notice inventory covers Pi and all distributed transitive dependencies.

**Rollback checkpoint:** do not promote the candidate. Keep the existing release channel and artifacts unchanged.

## Checkpoint 7 — opt-in distribution and eventual default

Initially distribute `merlin-next` as a separate opt-in package/channel. Do not replace stable Merlin
or reuse its update channel. Display build and runtime identity in both TUIs and logs.

Only after sustained acceptance and packaged-release success should a later, separately approved phase
make the rewrite the default. Even then, preserve a documented rollback package and an explicit user-driven
migration path; never silently rewrite stable state.

**Final promotion gate**

- Sustained successful acceptance runs across representative targets, not one successful demo.
- No open crash, data-loss, cross-installation interference, credential-exposure, or false-success blockers.
- Support, rollback, notice distribution, and release runbooks are complete.
- User approval to change the default command/channel is recorded separately from approval to build the rewrite.

## Binding design contracts from adversarial review

Codex reviewed this plan adversarially on 2026-10-31. The following contracts resolve its findings and
are part of the relevant checkpoints, not optional future hardening.

### Checkpoint 0 contracts

- Use exact paths: configuration and Pi state under `~/.merlin-next/config/`; data and run records under
  `~/.local/share/merlin-next/`; cache under `~/.cache/merlin-next/`; logs under
  `~/.merlin-next/logs/`; ephemeral IPC under `~/.merlin-next/run/`; browser profiles under
  `~/.merlin-next/browser-profiles/`. Next-specific environment overrides, when added, must be prefixed
  `MERLIN_NEXT_` and documented with path-precedence and owner-only permission tests.
- `merlin-next` has no daemon or embedded PostgreSQL. Only TUI-owned ephemeral child processes may use
  its unique IPC directory. Coexistence tests cover process termination and stale-file cleanup, not a
  next-daemon lifecycle. Stable Merlin's daemon and storage remain untouched.
- Perform the read-only capability disposition inventory here as design input, not as parity or a benchmark.
  Checkpoint 5 closes that inventory and proves release coverage.

### Checkpoint 1 contracts

- Put secret sanitization before model messages, Pi transcript persistence, telemetry, errors, tool output,
  and evidence. Tools exchange named secret references and allowlisted capture fields rather than raw
  credentials. Use owner-only secret storage and encrypted sensitive artifacts where raw capture is
  unavoidable. Define retention/deletion rules and run canary-secret tests across every output surface.
- Define a minimal local evidence format with immutable run/event IDs, monotonic sequence numbers,
  schema versions, atomic appends, integrity hashes, content-addressed artifacts, crash-tail recovery,
  bounded indexing, export/backup, retention, and explicit corruption errors. Files or embedded SQLite
  are acceptable; a database service is not.
- Define immutable `objective_id`, `run_id`, `session_id`, `branch_id`, and `evidence_id`. A fork creates
  a child run/evidence stream with parent references and inherited already-consumed aggregate budgets;
  it never appends to parent evidence. Compaction and run summaries are non-authoritative projections.
- Define a transport-neutral observer and deterministic mock observer. Checkpoint 1 resume tests use the
  mock; real browser/API freshness tests occur after those transports exist. Mutations remain blocked
  until a current observation is available.
- Pin the exact `@earendil-works/pi-mcp` package/extension integration. Reuse it rather than writing a
  manager, but test initialization, cancellation, streaming, sanitization, reconnect failure, and upgrades.
- Stop closes new-dispatch admission immediately, cancels cancellable work, and marks uncancellable
  in-flight effects unknown for reconciliation. Test pre-dispatch, transmission, post-acceptance, and
  parallel stop cases. Stop does not claim to undo remote effects.
- Bind every run to an immutable versioned scope covering hosts, principals, paths, methods, mutation
  classes, time bounds, and destructive-effect authority. Check it immediately before every target effect
  and copy its reference into evidence. Only an operator-authored revision and rebind can change scope.

### Discovery, transport, and verification contracts

- Checkpoint 2's API movement test uses a minimal direct API descriptor introduced there; Checkpoint 3
  expands capture, replay, proxy, and reporting. A browser-only journey does not require API execution.
- Stamp discoveries with `observed_at`, target/context binding, and invalidation reason. New endpoints,
  authentication rotation, navigation, level/state changes, and contradictory responses invalidate affected
  observations and trigger bounded rediscovery.
- Verification classes are authoritative target oracle, independent read-back, distinct-session
  reproduction, and operator attestation. Persist verifier identity, source, observation time, freshness,
  target binding, and confidence. A second model reading the same attacker response is not independent;
  stale or conflicting evidence remains uncertain.
- Reports contain sanitized reproduction recipes: request/payload templates, named secret references,
  prerequisite state, target/session constraints, success oracle, and artifact hashes. Test that recipes
  are structurally replayable without embedding credentials.

### Parallelism contracts

- One writer owns each physical mutable context and server-side state partition. Parallel workers require
  an explicit target/principal/resource partition key and evidence of independence; separate browser
  profiles alone are insufficient. Unknown independence means serialization. Reject stale snapshots and
  revoke write ownership on cancellation or completion without introducing durable lease infrastructure.
- Workers are ephemeral in-process task groups. No durable worker queue, replacement, review merge,
  worker-state recovery, or worker-owned authoritative state. Persist only intents, effects, observations,
  and terminal outcomes. Adding another worker lifecycle component requires design review.
- Test contamination through shared accounts, tenants, scores, rate limits, and server-side resources,
  not only through local browser state.

## Standing stop conditions

- A sixth nonterminal lifecycle state requires deletion/consolidation or demonstrated reliability justification.
- Any ordinary path blocked on a specialist is a defect.
- A new in-scope refusal needs a concrete authority or destructive-effect boundary.
- Any shared resource between stable and next requires an explicit design review and acceptance test.
- Any change to stable release artifacts, configuration, data, update channel, or command is outside this plan.
- If orchestration/recovery work exceeds attack-tool progress for a week, freeze infrastructure additions.
- Never declare a release from source tests alone; exercise the packaged application on macOS.

## Licensing and distribution checkpoint

Pi is MIT licensed, which permits commercial use, modification, sublicensing, and sale. A commercial
fork or bundled distribution must retain Pi's copyright and MIT permission notice in copies or substantial
portions. Keep upstream attribution, include third-party notices in source and packaged distributions,
and do not imply upstream endorsement or reuse Pi/Merlin branding without checking trademark rights.

Before each release, generate and review the complete dependency/license inventory. Model APIs, model
weights, fonts, assets, browser components, and other bundled runtime pieces have separate terms; Pi's
MIT license does not cover them. MIT has no general copyleft source-release requirement for proprietary
modifications, but distributed Pi-covered code and substantial portions must carry the required notices.

Authoritative sources:

- [Pi repository](https://github.com/earendil-works/pi)
- [Pi MIT license](https://github.com/earendil-works/pi/blob/main/LICENSE)
- [Pi coding-agent package](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json)
