# Merlin Harness Python

Lean Python/Textual rewrite of Merlin with a durable PostgreSQL control plane.

Merlin turns a plain assessment objective into a reviewed, evidence-bearing campaign:
reconnaissance first proves the target interaction contract, the operator reviews the exact sealed
manifest, a Campaign Lead admits and supervises the necessary specialist workers, findings are
independently reduced/validated, and terminal state produces an immutable report. It is an
authorized evaluation harness; it does not treat model prose as evidence or silently widen target,
tool, credential, or campaign authority.

Developer reference: [`docs/SOURCE-AND-PACKAGES.md`](SOURCE-AND-PACKAGES.md) maps the
application source, all workspace packages, runtime flows, durable state, and debugging entry
points.

## Run

Source development requires Python 3.13. The standalone macOS release includes its own Python,
PostgreSQL, and browser runtimes, and starts an app-managed PostgreSQL instance through
`pg0-embedded` by default. Set `MERLIN_DATABASE_URL` to use an existing server. Merlin never falls
back to SQLite.

```bash
uv sync --all-packages
uv run merlin
```

Provider configuration and credentials are created through `/setup`. Secrets are stored in the
owner-only Merlin credential store and are excluded from PostgreSQL, logs, reports, events, and
portable-memory exports.

Setup also records the provider's exact request dialect. OpenAI routes use
`max_completion_tokens`; DeepSeek and compatible routes that follow its API use `max_tokens` and
the explicit `thinking` control. Held-out promotion evaluations disable optional reasoning so
output limits, latency, and semantic scores measure the same deterministic request contract that
was reviewed. These evaluations are internal quality evidence; they do not gate configured routes.

Runtime tuning lives in `~/.merlin-python/config.toml`. Cross-campaign negative evidence is reused
only when the target, reviewed adapter, surface, technique, transform, language, and oracle all
match; findings, drift, expiry, and setup-only outcomes are revalidated. The validity window is
explicit and bounded:

```toml
[runtime]
learning_evidence_validity_seconds = 86400
```

Persistent authenticated browser state is operator-scoped by exact origin. Merely recognizing a
hostname never enables or labels an authenticated profile. Configure both the private profile
directory and every reviewed origin that may use it; all other origins receive isolated contexts.

```toml
[browser]
profile_directory = "/absolute/owner-only/browser-profile"
persistent_origins = ["https://target.example"]
```

The same setting can be written atomically without editing TOML:

```bash
merlin browser profile /absolute/owner-only/browser-profile https://target.example
merlin restart
```

Heterogeneous model routing is optional and operator-evaluated. Omit these tables to use the one
configured model everywhere. Category routes may be overridden by an exact specialist profile;
the resolved Lead and worker routes are shown in campaign review before approval.

```toml
[provider]
model = "homogeneous-fallback"

[provider.role_models]
lead = "high-reliability-model"
recon = "vision-tool-model"
routine = "economical-tool-model"
validator = "independent-validator-model"
# injection = "exact-profile-override"

[provider.escalation_models]
routine = ["stronger-model", "strongest-model"]

[provider.model_capabilities]
vision-tool-model = ["vision", "tools"]
economical-tool-model = ["tools"]
# An empty exact entry explicitly denies image input for this model.
catalog-vision-model = []

[provider.model_pricing.high-reliability-model]
input_cost_per_million = 3.0
output_cost_per_million = 12.0

```

For `model_capabilities`, presence is authoritative. An exact entry containing `vision` enables
image input, while an exact entry without `vision` (including `[]`) disables it. Omit the model key
to let reviewed provider/catalog metadata determine image capability. The Provider Setup checkbox
persists the checked or unchecked choice as an exact per-model override.

Per-model pricing overrides the provider-wide pricing used by homogeneous configurations. A
cost-capped route without either price fails closed. Before reconnaissance, route preflight checks
that every required transport is configured and reachable and that the selected model exposes the
capabilities required by its role. Campaign review shows the resolved model, transport,
capabilities, limitations, and fallback order. Merlin does not require route certificates or an
expiry/renewal ceremony to use a configured model.

```toml
[evaluation]
suite_files = ["/absolute/operator-owned/path/provider-heldout-v1.json"]
```

Suite files are bounded, owner-controlled, non-symlinked JSON documents whose declared dataset
and per-fixture content digests are revalidated at startup. Each suite declares an exact
`candidate_kind`; omitted default selection chooses only a compatible suite, and an explicit
wrong-domain suite fails before any provider or target action. Routing candidates can use immutable
`file://` provider fixtures: Merlin sends the exact suite model through the configured production
route with no tools, bounds time/events/output, checks exact and forbidden output oracles, requires
usage when requested, records latency/cost/authority violations, and can cancel a non-terminal
stream before proving a fresh recovery request works. Deterministic smoke fixtures are not evidence
of production reliability. HTTP target-adapter candidates use a separate immutable fixture contract bound
to the exact adapter identity, interface, endpoint, credential handle, and suite route. Merlin
resolves the opaque credential locally, executes the bounded start/message/final-cleanup lifecycle,
checks exact JSON-pointer witnesses, captures reviewed dynamic values such as a generated session
ID for exact typed substitution into later request templates, and replays declared ambiguous-result
steps with the same idempotency key. `response_pointer: ""` selects the whole JSON response (the
JSON Pointer root); nested selection starts with `/`. Browser-adapter fixtures and authorized external-target
evaluation use a separate immutable browser contract. Browser cases open the reviewed endpoint
first, run only bounded Merlin browser tools inside a one-tab exact-origin isolated context, carry
the authorized console session into browser ownership, chain reviewed observation values such as
opaque tab references, and close the whole context after success, failure, timeout, or cancellation.
An authenticated browser fixture may name only the opaque built-in handle
`browser-profile:configured`. Production resolves it through the shared exact-origin browser
service, never through a suite filesystem path or exported cookie/token. Evaluation serializes on
the persistent actor lease and releases that actor after success, failure, timeout, or cancellation
without closing the durable profile. An authorized external-target evaluation run remains an
incomplete distribution boundary.

Network evaluation is never authorized merely because a suite file exists. `/promotion evaluate`
binds the current durable conversation session into the permission engine: routing suites require
`provider_network`, adapter suites require `target_network` for the candidate's exact endpoint,
and a missing or denied authorizer fails before fixture loading or external execution.

```text
/promotion evaluate ROUTING_CANDIDATE HELD_OUT_SUITE
/promotion evaluate ADAPTER_CANDIDATE HELD_OUT_ADAPTER_SUITE
/promotion accept ROUTING_CANDIDATE "reviewed held-out result"
```

Before installing a suite into the durable promotion workflow, an operator can execute one or more
owner-controlled immutable provider fixtures against an exact production route:

```bash
uv run --package merlin-tui python scripts/acceptance_provider_route.py \
  /absolute/path/to/provider-case-1.json /absolute/path/to/provider-case-2.json \
  --model exact-model-id \
  --provider-name configured-provider \
  --route exact-route-id \
  --role-scope routine
```

The command evaluates every positional fixture through one provider instance and reports both
per-case and production-identical aggregate metrics. Native routes add
`--transport codex_app_server`, `--transport claude_cli`, or
`--transport kimi_acp`. The command reports the fixture SHA-256, exact provider/model/route,
bounded quality, attribution, authority, latency, usage-cost, and cancellation-recovery metrics;
raw input/output/total tokens remain visible even when a native or subscription route has no
configured dollar price; it exits nonzero on task failure or a tool-authority violation. This is
an optional internal benchmark, not a prerequisite for routing a configured model.

`--role-scope` may be repeated with `lead`, `recon`, `routine`, or `validator`; omit it only when
the corpus genuinely qualifies the route for `all` roles. `all` cannot be combined with another
scope. A routing candidate and frozen suite must declare the same scopes so benchmark results are
not generalized beyond the roles actually evaluated.

Provider fixtures use `merlin.provider-evaluation.v1` for no-tool decisions or
`merlin.provider-evaluation.v2` when route evaluation requires a bounded dynamic
tool. V2 seals the exact advertised function schemas and an ordered `expected_tool_calls` transcript
containing each call's name, arguments, reviewed output, and success flag. The evaluator returns
only those reviewed outputs and fails the case on a missing, extra, reordered, renamed, or
argument-drifted call. This proves the model can use a narrowly projected tool; it does not grant
generic browser, shell, MCP, workspace, or target authority, and it does not by itself establish
vision unless the corpus includes a separately supported image boundary.

After reviewing a baseline run, add `--write-suite /absolute/new-suite.json --suite-id NAME` to
create (never replace) the owner-only frozen suite consumed by `[evaluation].suite_files`. For an
unpriced route, also provide an explicit positive `--maximum-token-ratio`. The measured aggregate becomes the immutable baseline, so
the resulting production evaluation detects regressions against the reviewed run rather than an
invented benchmark. `--maximum-latency-ratio` defaults to the strict production value `1`; raise it
explicitly when the reviewed route policy permits bounded latency variance.
The preflight event ceiling defaults to the production maximum of 100,000 because reasoning-capable
routes may emit many small deltas even for a short final answer; `--max-events` can lower that bound.

For an unpriced native/subscription route, the suite policy must set a positive
`maximum_token_ratio` and both baseline and observed evaluation metrics must contain nonzero token
usage. A zero-dollar/zero-dollar comparison is not cost evidence. Priced
routes continue to use `maximum_cost_ratio`; suites may include both gates.
When a Lead follow-up cites a failed or quarantined source task, the first distinct configured
escalation model is used in a new attributable wave/task generation. The fallback order is shown
in campaign review; an in-place retry never silently changes models.

Preparation seals the skip/revalidation decisions into the manifest and shows their counts in the
campaign review before approval. If all admitted cells would be skipped, one cell is retained for
revalidation so prior evidence cannot silently terminate a new assessment.

## Verify

```bash
uv sync --all-packages
uv run python scripts/verify.py phase 12
uv run python apps/merlin-tui/scripts/v01_release_gate.py
```

The v0.1 gate exercises production composition, real configured-provider/Textual swarms, local and
authenticated remote MCP, browser automation, durable specialists, findings and reports,
attributable restart-safe worker-to-Lead/peer coordination,
permissions, portable memory, promotion/rollback, restart recovery, readiness, and shutdown. It
fails if a configured credential value appears in captured output.

Phase verification is intentionally scoped and time-bounded. It runs formatting,
lint, and only the tests belonging to that phase.

## macOS 14 ARM installation artifact

```bash
make release
```

This creates and smoke-tests a standalone archive at
`target/releases/merlin-0.1.0-darwin-arm64.tar.gz`. A recipient does not install Python, `uv`, or
the source tree:

```bash
tar -xzf merlin-0.1.0-darwin-arm64.tar.gz
cd merlin-0.1.0-darwin-arm64
./merlin
```

The first launch installs Merlin for the current user at `~/.local/bin/merlin` and immediately
opens the TUI. If no provider has been configured, Merlin starts the guided provider, model, and
hidden API-key setup first. Later launches can use `merlin` directly and skip setup unless the user
runs `/setup` again. First setup also offers an optional numbered Campaign Lead route through Codex
App Server, Claude CLI, or Kimi ACP; pressing Enter keeps the provider model for every role.

Use `/model` to change the OpenAI-compatible default model or to configure a native no-tool
transport for Campaign Lead. The model dialog exposes Codex App Server, Claude CLI, and Kimi ACP;
native CLI transports must be scoped to Campaign Lead so specialists retain Merlin-owned tools.
Kimi ACP cannot be used when the campaign seals an explicit token budget because ACP reports
context occupancy rather than billable per-turn usage. Restart Merlin after changing a route.

Run `make install-release` to build, test, and install the same release as
`~/.local/bin/merlin`. The wheelhouse remains an internal, offline-install artifact.

The standalone launcher provides user-facing lifecycle commands:

```bash
merlin start
merlin stop
merlin restart
merlin status
merlin update
merlin uninstall             # purges Merlin data by default
merlin uninstall --keep-data # preserves ~/.merlin-python
```

For scripts and CI, plain mode streams only assistant content while NDJSON emits every durable
event with a monotonic sequence and terminal `end` record. Tool effects remain denied unless the
operator explicitly supplies `--approve`; stdin and durable-session resume are supported:

```bash
merlin --plain "Summarize current campaign status"
printf '%s\n' "Assess the reviewed target" | merlin --ndjson --approve --session SESSION_ID -
```

Both modes return a non-zero exit status on an error event or transport failure. Secrets and API
keys are never included in the event envelope.

Active work can be stopped hierarchically from the TUI:

```text
/cancel all
/cancel campaign <campaign-id>
/cancel task <task-id>
/pause campaign <campaign-id>
/resume campaign <campaign-id>
/campaign status <campaign-id>
/campaign saturation <campaign-id> stop
/campaign saturation <campaign-id> continue
/campaign saturation <campaign-id> limit <additional-waves>
```

Campaign cancellation stops its active task schedules and preserves required cleanup. Task
cancellation stops only the selected durable worker task; the campaign lead can continue reducing
the remaining results. Pause/resume is durable across daemon restart. Compatibility commands
(`/agents kill all`, `/campaign cancel`, `/campaign pause`, and `/campaign resume`) remain
available. Exact campaign and task IDs also work from a different attached console.

Campaign limits are optional and unlimited by default. The non-interactive submit command accepts
positive turn, token, wall-clock, and USD cost limits; Merlin passes them through reviewed preparation and
shows the exact sealed values before approval:

```bash
merlin campaign submit --profile exhaustive \
  --max-turns 500 --max-tokens 2000000 --max-seconds 21600 \
  --max-cost-usd 25 \
  "Assess the reviewed target"
```

An interactive objective may request the same limits in natural language. A cost cap requires
attributable `input_cost_per_million` and `output_cost_per_million` values under `[provider]` in
the centralized configuration; unresolved pricing fails closed. Merlin reserves against the higher
configured rate and settles the provider-reported actual cost. The campaign does not
execute until the deterministic preview shows those exact limits and the operator approves its
manifest digest. Status and terminal reports use the same sealed limits.

An exhaustive campaign does not stop merely because the Lead says coverage is complete. Merlin
requires three quiet waves, every sealed catalogue cell attempted, the reserved typed-hypothesis
cells attempted, all tasks terminal, and no pending validation. At that boundary `/campaign
status` opens a numbered choice: stop and generate the report (default), continue for another
frontier wave, or continue for an explicit number of additional waves. The request and response
are journaled, so another attached client or a restarted daemon observes the same pending choice.

## Reports

HTML is the default report and includes a sticky contents navigator, executive verdict and
confidence, outcome visualization, coverage counts, per-technique rationale, independent
reproduction status, evidence/target exchanges, and pack-to-exchange lineage. Markdown preserves
the same navigable evidence structure. CSV is a relational ZIP rather than a lossy flat file and
contains summary, findings, techniques, evidence, exchanges, lineage, unsearched-frontier, and
language-coverage tables. JSON is available for machine integration.

```text
/report
/report --format markdown --audience internal --output ./report.md
/report --format csv --audience customer --output ./report-csv.zip
/report compare OLDER_REVISION NEWER_REVISION
```

Deterministic representative outputs live in [`examples/reports`](../examples/reports/README.md).
Report filenames are UTC-prefixed and immutable revisions remain comparable. Customer exports
apply the customer-redaction policy; internal reports retain attributable evidence.

## Current support boundary

- Supported distribution target: macOS 14+ on Apple silicon. Other platforms are intentionally
  deferred until this boundary is clean.
- Supported source-runtime transport: one configured OpenAI-compatible endpoint with exact
  per-role model selection plus opt-in bounded Codex App Server, Claude CLI, or Kimi ACP routing
  for no-tool Campaign Lead work. Tool-bearing workers remain on the Merlin-owned compatible
  endpoint. Live CLI prompt evaluation and a newly requested release still gate distribution.
- Campaigns are single reviewed target sessions. Automatic fresh-session recycling is deliberately
  deferred; terminal target state closes that campaign frontier.
- Campaign turns, cumulative tokens, and wall-clock duration are unlimited unless the operator or
  profile seals a limit. Provider requests, individual tool actions, worker leases, retries, and
  the lenient per-turn semantic-step guard remain finite so dead or looping work is recoverable.
- Exhaustive completion requires attributable admitted-cell coverage and a durable saturation
  decision. English-only execution cannot close a reviewed multilingual frontier.
- Multiple TUI/plain/NDJSON clients may attach to the same local daemon. The daemon owns the shared
  PostgreSQL control plane; clients do not each start an independent scheduler.
- The encrypted eight-pack inventory is bundled and integrity/authenticity checked. Workers can
  discover the inventory but receive only the skill, tool, MCP, pack projections, and authority
  compiled for their specialist role.

## License

First-party code and all eight bundled knowledge packs are licensed under
[Apache-2.0](../LICENSE). See [licensing details](LICENSING.md) for third-party components.
