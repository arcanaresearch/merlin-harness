<div align="center">

<img src="assets/merlin-mark.png" alt="Merlin logo" width="96">

# Merlin

### Conversation-first AI security testing

**Built for: Chatbots · AI agents · RAG applications · Tool-using AI · Multi-agent systems**

Tell Merlin what you are authorized to test. It learns how the application works, proposes an
offensive campaign for review, follows promising evidence, and returns evidence-backed findings
with clear evidence status and coverage gaps.

**72 attack cases · 14 benign controls · Browser + API · Adaptive exploration · Heterogeneous models**

[Quick start](#quick-start) · [How it works](#how-merlin-works) ·
[What makes it different](#what-makes-merlin-different) · [Current coverage](#current-offensive-coverage)

</div>

![Merlin coordinated security assessment](assets/merlin-hero.png)

## Optional browser workspace

A pb2-style web workspace is available from source for chat, campaign review, agents, coverage,
reports, setup and advanced controls. See [run the browser workspace](docs/web-interface.md).

## Watch the CLI walkthrough

[![Watch the 106-second Merlin CLI walkthrough](assets/merlin-cli-walkthrough.jpg)](https://github.com/triwiz-ai/merlin-harness/releases/download/v0.2.0/merlin-cli-walkthrough-v2.mp4)

See Merlin connect models, define an authorized target, review a campaign, coordinate agents,
accept operator guidance, and produce an evidence-linked report.

![Merlin conversation-first assessment interface](assets/merlin-welcome.png)

> [!NOTE]
> Merlin is under active development. Standalone releases currently target macOS 14 on
> Apple silicon. First-party code and all eight bundled knowledge packs use Apache-2.0.

Merlin includes adversarial policy-control guidance for commercial price authority, binding
commitments, synthetic confidentiality canaries, and customer or tenant isolation. These controls
require target-specific reviewed policy bindings, benign controls, and independent target-state or
receipt evidence. Merlin validates their typed evidence contracts and projects their authenticated
pack identities separately from advisory knowledge. They help test defined controls; they do not
certify legal or regulatory compliance, and a bounded negative result must not be reported as
organization-wide compliance.

> Use Merlin only against targets you are authorized to assess.

## See Merlin investigate

```text
You
  Assess the reviewed support agent for prompt injection, cross-user data access,
  and unsafe tool use. Do not perform destructive actions.

Merlin
  1. Learns the target's browser, session, and API behaviour
  2. Proposes the exact campaign, team, models, tools, limits, and cleanup
  3. Waits for your review
  4. Runs applicable attacks with matched benign controls where the target supports them
  5. Follows useful clues and records attributable evidence
  6. Returns findings, evidence status, coverage gaps, and what was never tested
```

If you already use Codex or Claude Code, the interaction should feel familiar: describe the job in
ordinary language, review the proposed work, and steer it through a conversation. Merlin applies
that workflow to an authorized security investigation.

## Install and run

To run Merlin from source, install Python 3.13+ and `uv`, prepare the workspace once, then use the
launcher in the repository:

```bash
git clone https://github.com/triwiz-ai/merlin-harness.git
cd merlin-harness
uv sync --all-packages --locked
./merlin
```

That is the normal launch command—there are no flags to remember. On the first launch, Merlin
notices that no provider is configured and walks you through choosing a provider, model, and API
key before opening the app. Later launches go straight into Merlin. Use `/setup` only when you want
to change that configuration.

Merlin stores credentials in its owner-only credential store rather than in PostgreSQL, prompts,
logs, reports, or exported memory.

To produce the standalone macOS bundle, run:

```bash
make release
cd target/releases/merlin-0.2.1-darwin-arm64
./merlin
```

To increment the workspace version and build the corresponding release in one command, use
`make release-patch`, `make release-minor`, or `make release-major`.

The bundle includes its own Python, PostgreSQL, and browser runtimes. Its first launch installs
Merlin for the current user and opens the same guided setup when needed.

### Lead providers and worker models

Use `/model` to select a native **Codex App Server**, **Claude CLI**, or **Kimi ACP** Lead,
or configure a hosted or local model through `/setup`. Native CLI integrations require their
respective local installation and authentication. The API-provider picker lists model endpoints;
CLI Lead choices live in the Lead/model selection flow. Specialist workers use Merlin's own tool
execution and reviewed model routes.

### Optional JEV judgments

Merlin can call TypeSafe's **JEV** judgment service for typed decisions such as refusal grading
and triage. Guided setup offers this separately from the campaign model; you can keep it off.
Open `/setup` and choose **JEV — judgment API key**, or use `/setup jev` directly.
Merlin saves the key securely and reports the health-check result. `/model` exposes the
judgment toggle afterward.

JEV supplies judgments and confidence at implemented decision points. It does not own campaign
authorization, target-session scheduling, or cleanup. Some routing and capability judgments remain
shadow evaluations. Missing or failed judgments fall back to the existing handling, with
uncertainty preserved rather than silently removing authorized tests.

## Why Merlin?

A common AI security workflow sends a prepared list of jailbreak or attack prompts, scores the
responses, and stops when the list is finished. That is a useful baseline, but a real AI product
also has users, sessions, APIs, tools, memory, workflows, and sometimes the power to take actions.

Merlin starts with a reviewed catalogue too. It then adds reconnaissance, matched benign controls,
continuous learning, adaptive exploration, specialist coordination, and application evidence.

| Catalogue-based scan | Merlin |
|---|---|
| Starts with prepared prompts | Learns the target, then runs a reviewed attack frontier |
| Runs the predefined list | Can develop target-specific hypotheses and mutations |
| Treats tests independently | Carries evidence and state between campaign steps |
| Primarily scores responses | Compares attacks with controls and target-state evidence |
| Uses one general execution path | Can route different jobs to different models |
| Starts each scan from zero | Reuses comparable lessons and revalidates stale evidence |
| Reports interesting output | Reports findings, evidence status, coverage, and lineage |

## How Merlin works

```mermaid
flowchart LR
    O["Conversation<br/>objective + boundaries"] --> R["Reconnaissance<br/>learn the target"]
    R --> P["Campaign proposal<br/>team + routes + limits"]
    P --> V{"Operator review"}
    V -->|approve| S["Specialist campaign"]
    V -->|revise| P
    S --> A["Attack"]
    S --> C["Benign control"]
    A --> E["Evidence + oracle"]
    C --> E
    E --> F["Finding + validation state"]
    E --> L["Durable lesson"]
    L --> X["Next attack,<br/>mutation, or hypothesis"]
    X --> S
    F --> Q["Evidence-linked report"]
```

1. **Describe the job.** Name the target, what you want to learn, and what must remain off-limits.
2. **Learn the target.** Reconnaissance maps the reviewed browser or API interaction before attack
   execution begins.
3. **Review the campaign.** Inspect the techniques, specialist composition, model routes, tools,
   budgets, evidence requirements, and possible effects.
4. **Run and steer.** Merlin executes admitted work, follows useful evidence, and lets the operator
   pause, redirect, resume, or stop it.
5. **Check the evidence.** Findings remain tied to the request, response, observed state, control,
   oracle, worker, and campaign revision.
6. **Learn.** Comparable results can shape later work without turning old evidence into permanent
   truth.

## What makes Merlin different

### Reconnaissance before attack

Merlin does not require the operator to explain every selector, endpoint, session transition, or
response field. It first performs a bounded, harmless reconnaissance pass and produces a reviewed
interaction contract for the campaign.

### Test beyond the advertised feature list

A security scanner should not assume that an undocumented or unsupported surface is safe. When
Merlin has an authorized, harmless way to deliver and observe a probe, it can investigate unknown
upload, RAG, memory, multimodal, tool, agent-message, and language surfaces. If a surface cannot be
reached, the report records that limitation instead of silently removing it from coverage.

### Exploration beyond the catalogue

The built-in catalogue is a starting point, not the edge of the investigation. Exploration can:

- create falsifiable hypotheses from target evidence;
- propose observation-derived neighbouring tests;
- generate new arms through brainstorming;
- route around evidenced failures;
- generalize successful attacks;
- apply reviewed register and executable encoding mutations; and
- reject duplicate or near-duplicate ideas before they consume target budget.

Merlin also contains additional arm-generation building blocks that are not all connected to the
live supervisor yet.

### Continuous learning without blind trust

Merlin records what was attempted, the target and scenario where it happened, the outcome, and the
evidence behind it. During an Autonomous campaign, it repeatedly ranks the remaining attack
frontier, analyzes committed results, develops mutations or neighbouring tests, and can reshape the
specialist team while useful evidence continues. On a later comparable campaign it can also:

- avoid repeating a still-current exact negative;
- recheck a previous finding;
- revalidate work after target, adapter, oracle, language, or evidence-age drift;
- prioritize a related but untested variation; and
- reuse an evidenced interaction contract.

Evidence from another target or scenario may inspire a hypothesis. It cannot silently prove a
finding, suppress a test, or count as coverage.

### One campaign, different brains

Merlin is designed for heterogeneous model routing because each responsibility has a different
workload. Reconnaissance usually receives the most input—page structure, browser traffic, API
descriptions, target metadata, and accumulated observations—so it should normally use a model with
a large context window. That may not be the best or most economical model for leading the campaign,
running routine attacks, exploring unusual clues, or validating a finding. The resolved routes and
fallback order remain visible during campaign review.

```text
Campaign Lead         → high-reliability reasoning model
Reconnaissance        → large-context, vision/tool-capable model
Routine workers       → economical execution model
Adversarial explorer  → creative exploration model
Validator             → separately selected checking model
Escalation            → stronger reviewed fallback
```

A single sufficiently capable model can still run every role, which makes initial setup easier.
For serious campaigns, the recommended arrangement is to route each responsibility to a suitable
model—especially a large-context model for reconnaissance and a separately selected model for
validation.

### Compare attacks with normal behaviour

A suspicious response is not automatically a vulnerability. Where the target and technique support
it, Merlin pairs an attack with a reviewed benign control and checks both against an explicit oracle.

```text
Control: User A reads User A's conversation
Attack:  User B requests the same conversation ID
Oracle:  Compare the responses and resulting application state
```

If the normal action fails too, Merlin should not confidently report an authorization flaw.

### Keep uncertainty instead of hiding the test

Merlin does not let its own content evaluator silently suppress an authorized attack. Target-owned
evidence takes priority; when the oracle, metadata, or evaluator disagrees, Merlin preserves the
attempt and marks the result uncertain for validation, mutation, or retry. Evaluator uncertainty is
not reported as a target failure, a clean result, or completed coverage.

### A team that changes with the investigation

Choose how the campaign starts:

- **Opinionated:** start from Merlin's ready-made Authentication and Session, Injection, Data
  Exposure, Business Logic, and Capability Abuse template.
- **Autonomous (default):** let the Campaign Lead compose the initial specialists from reconnaissance, then
  propose mutations, neighbouring tests, and specialist changes after each evidence-bearing wave.

Both modes use the same dynamic campaign engine. Reconnaissance, Campaign Lead, validation,
remediation, automation authorship, automation review, and deterministic execution remain separate
responsibilities. The Lead may change the investigation; it cannot grant itself a new target,
tool, account, or budget. Campaign completion is decided from durable progress, remaining work,
infrastructure state, cleanup, and reviewed limits—not merely because the Lead says it is finished.

## Quick start

1. Run `./merlin`. If this is your first launch, Merlin guides you through setup automatically.
2. Describe an authorized target and objective in ordinary language.
3. Let reconnaissance establish how the target works.
4. Choose Recommended (default), Quickscan, Exhaustive, or Exploration/learning coverage, then
   Autonomous (default) or Opinionated agent composition.
5. Review the sealed campaign before anything executes.
6. Approve, then watch and steer the investigation.
7. Open the finding evidence and export the report.

Example objective:

```text
Assess https://target.example for indirect prompt injection, cross-session data
exposure, and unsafe tool use. Use only the approved test account. Do not create
external side effects, and stop if the target shows degradation.
```

## See the campaign in Merlin

These TUI captures use sample campaign data. They illustrate the interface; labels and defaults
may differ by version. The findings and outputs shown are not benchmark results.

### Choose coverage and team

Coverage and agent composition are separate choices. Recommended is the default and provides
representative coverage; Quickscan offers a smaller starting campaign, Exhaustive broadens it,
and Exploration/learning emphasizes investigation. Autonomous is the default agent composition;
Opinionated uses the reviewed team template.

![Choose a scan profile](assets/merlin-choose-profile.png)

![Choose the initial specialist team](assets/merlin-choose-team.png)

### Review before execution

Inspect the proposed target scope, work, model routes, limits and cleanup before confirming.

![Review and confirm a campaign](assets/merlin-campaign-confirmation.png)

### Follow workers in the main conversation

The inline swarm strip shows individual workers, current tasks, latest retained output, queued
work and failures. Its orange working indicator animates during execution and becomes static
when paused or terminal. Campaign state and worker history are restored when resuming a session.

![Inline agent swarm summary](assets/merlin-agents-summary.png)

### Inspect agents in a full-screen workspace

Open `/agents` or click the swarm strip. A compact assignment list identifies the specialist handling each task beside live output panes;
Each pane shows the recorded specialist, worker owner, task, activity, and retained output.
Failed assignments remain inspectable after the campaign ends.

- **A:** open the agent overview, grouped by recorded agent identity, with current activity, model, task history and latest output. **T / Esc:** return to the task workspace.
- **Mouse drag:** select detail text. **Ctrl+Shift+C:** copy the selection. Live text updates pause while selecting.
- **Arrow keys:** select a worker.
- **Tab / Shift+Tab:** move between visible worker panes.
- **Space:** pin a worker while inspecting others.
- **Enter:** expand the selected worker's retained transcript, attempts, output and failure details.
- **Esc:** leave the expanded transcript, then return to the conversation.

![Full-screen agent workspace with individual output panes](assets/merlin-agents-workspace.png)

![Agent overview grouped by recorded identity](assets/merlin-agents-overview.png)

![Expanded worker transcript and failure details](assets/merlin-agent-transcript.png)

The transcript shows retained assistant and tool messages. Messages removed by context compaction
are not reconstructed. Shared target sessions use reviewed capacity; work waits for admission
rather than competing for a session owned by another assignment. Historical infrastructure
failures remain visible and do not become successful coverage.

### Finish, export and resume

The main screen replaces live progress with a persistent scan summary: completion or partially completed
status, findings, coverage, cleanup and the HTML report path. When execution stops early, the summary shows partial completion and the stop reason,
with saved work and remaining coverage visible.

HTML is exported automatically. Use the summary's export buttons or `/report --format …` for
Markdown, JSON and CSV; these additional formats are generated on request. Open HTML and Reports
actions make the result accessible without searching the filesystem.

![Main-screen completion summary and report export actions](assets/merlin-campaign-complete.png)

`/report` opens a full-screen campaign picker with **Open HTML**, **Copy HTML path**,
and exports for **Markdown**, **JSON** and a **CSV ZIP bundle**. Existing files appear
in the selected campaign details. Exports use that saved campaign, including historical
campaigns, rather than whichever session happens to be open.

![Report browser with saved files and export actions](assets/merlin-report-browser.png)

The HTML report brings findings, coverage and recorded evidence together in a browser.

![Color HTML report showing findings and assessment coverage — representative sample](assets/merlin-html-report.png)

*Representative sample report.*

![Color report coverage chart and outcome breakdown — representative sample](assets/merlin-html-report-coverage.png)

If you quit halfway and resume the saved session, Merlin restores durable campaign checkpoints,
worker history and progress. When that resumed campaign ends, it uses the same summary and report
flow. Resuming an already finished session restores its saved report summary and actions.
Partially completed campaigns keep their HTML report and export actions. **Recover** resumes
unresolved coverage from the saved wave boundary while preserving completed waves; it can also
rebuild a missing report from durable state. Target sessions still need to be available for new
requests, and retained checkpoints do not undo external target changes.

Completion does not mean every test succeeded: the report records failed, blocked, uncertain and
untested work explicitly.

## From clue to finding

Merlin is designed to preserve the complete reasoning boundary around a result:

```text
Reconnaissance observation
  Browser identity and conversation ID appear as separate request values
        ↓
Target-specific hypothesis
  The API may trust the conversation ID without checking the signed-in user
        ↓
Reviewed experiment
  Permitted control + cross-user treatment + explicit oracle + cleanup
        ↓
Evidence
  Exact requests, responses, selected state, receipts, and target identity
        ↓
Disposition
  Finding, negative result, inconclusive, blocked, or not attempted
```

## Current offensive coverage

The built-in catalogue currently contains **72 offensive test cases** paired with **14 reusable
benign controls**, for **86 total cases**. A case is one concrete combination of attack dimensions,
not necessarily a unique technique family.

- Direct and indirect prompt injection
- Multi-turn Crescendo, GOAT, and Hydra-Select strategies
- Authentication, session, identity, and tenant boundaries
- Data exposure, cross-user access, and extraction
- Business-logic, sequencing, replay, and state-machine abuse
- Tool, agent, external-effect, and capability abuse
- RAG, stored content, memory, and prior-output reinjection
- Context derailment, Poison Soup, token/output amplification, and resource-abuse guidance
- Multilingual, register, encoded, and obfuscated variants
- Browser, JSON API, and reviewed hybrid flows

Catalogue membership does not mean every attack is applicable or executed against every target.
Reports distinguish admitted, attempted, blocked, inconclusive, negative, and untested work.

## Current limitations

What remains incomplete:

- General automatic cross-user, cross-session, cross-tenant, object-ID, method, and content-type API
  mutation is not yet a universal campaign capability.
- Complete scan-time token, latency, tool-call, recovery, and denial-of-wallet comparison is not yet
  available for every campaign.
- Generated custom Python or Playwright code has no approved sandboxed runtime.
- Not every arm-generation helper is wired into live campaign scheduling.
- Fresh isolated-session reproduction is not available for every target and finding.
- Comparative coverage is still narrow: current head-to-head evidence comes from local,
  non-official reproductions rather than official leaderboard submissions.
- Packaged support currently targets macOS 14 on Apple silicon.

## Benchmarking

Merlin is benchmarked as an adaptive attacker that finds real, executable vulnerabilities under
a fixed budget — not as a single dataset or one ASR number. Local comparative reproductions now
include RIFT-Bench and MUZZLE + The Zoo. RIFT compared one probe with Merlin on flow 0 of one probe
family across 22 runnable systems, with DeepSeek replacing Azure victim/judge deployments and with
dependency/provider adaptations. MUZZLE and Merlin each ran one adapted Gitea scenario against the
same `deepseek-chat` victim and the same MUZZLE ground-truth judge, but used different attacker
models and round counts.
Jailbreak Foundry and AgentHarm results measure DeepSeek victim susceptibility, not Merlin
performance. AIRTBench and REDAgentBench remain deferred; REDAgentBench/DianJin-RED benchmark data
is CC BY-NC 4.0 plus applicable upstream terms.

**These are local, non-official reproductions using DeepSeek and disclosed benchmark adaptations,
not official leaderboard submissions.** Denominators and evaluation contracts differ by track.
See [the benchmark results and methodology boundaries](docs/benchmarks.md); raw benchmark and
vendor outputs are not included in the release.

## Security and responsible use

Merlin is an offensive assessment harness for authorized testing. Models, target content, tools,
imported guidance, and generated procedures are treated as untrusted inputs. Deterministic code
retains authority over targets, credentials, tools, budgets, consequential actions, and cleanup.

Do not use Merlin against systems you do not own or have explicit permission to test. Report Merlin
security issues privately to the maintainers rather than including sensitive details in a public
issue.

## Community and project status

Merlin is pre-release software. Issues, test targets, attack ideas, adapter contributions, and
reproducible evaluations are welcome. Claims about finding yield should include the target, model,
scope, tools, budget, evidence standard, and comparison conditions.

First-party code and all eight bundled knowledge packs are licensed under
[Apache-2.0](LICENSE). Third-party components retain their own licenses; see
[licensing details](docs/LICENSING.md) and the [dependency inventory](docs/THIRD-PARTY.md).
For development setup and checks, see [Contributing](docs/CONTRIBUTING.md),
[the development reference](docs/DEVELOPMENT.md), and [source architecture](docs/SOURCE-AND-PACKAGES.md).


### Session and campaign controls

`/resume` restores a saved conversation and reconnects its campaigns. `/campaign resume ID`
continues one paused campaign within the current session; it does not switch conversations.
`/interrupt` interrupts the current conversation turn and any swarm attached to that turn.
It does not explicitly cancel a durable campaign by ID; use `/campaign cancel ID` for that.
`/cancel task ID` cancels one task while other campaign tasks can continue.
`/campaign cancel ID` cancels that campaign's work and retains its evidence.
`/quit` closes Merlin cleanly. In `/agents`, press **A** for the grouped agent overview
and **T** to return to the task workspace.
