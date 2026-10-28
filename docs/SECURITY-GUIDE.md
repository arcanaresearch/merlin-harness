# Merlin security guide

**Conversation-first AI security testing.**

This document explains what Merlin is, how its security model works, what it can do today, and what
we are building next. It is written for security teams, technical reviewers, partners, and anyone
who wants more detail after hearing the short pitch.

For a meeting-friendly version, use [`SECURITY-MEETING-PITCH.md`](SECURITY-MEETING-PITCH.md).

## What Merlin is

Merlin helps security teams test AI agents the way an attacker would.

It is conversation-first: the operator describes the assessment in plain language, reviews the
campaign Merlin proposes, and steers the investigation as evidence comes back.

The operator tells Merlin which system they are allowed to test and what they want to investigate.
Merlin first learns how that system works, prepares a campaign for review, and then runs attacks
through the browser or API. When something interesting happens, it keeps the requests, responses,
target changes, and other evidence needed to check whether the issue is real.

Merlin is meant to find useful security issues, not simply produce a long compliance checklist or
count how many jailbreak prompts received an unsafe-looking answer.

## Why we are building it

An AI application is usually more than a model and a prompt. It may include:

- user accounts and permissions;
- browser sessions and API tokens;
- tools that send email, issue refunds, update records, or run code;
- uploaded files and retrieved documents;
- long-running conversations and stored memory;
- approval and escalation flows; and
- usage limits, provider costs, and background jobs.

A static prompt benchmark can test some model behavior, but it cannot fully answer questions such
as:

- Can one user access another user's conversation?
- Can a poisoned document affect a later user?
- Can the agent be tricked into using a tool without approval?
- Can an attacker make it repeatedly perform expensive work?
- Does the application recover correctly after an interrupted or oversized request?
- Can several weak behaviors be joined into a practical attack?

Merlin treats the assessment as an investigation of the complete AI application.

## The short version of how it works

```text
authorized objective
  -> understand the target
  -> review the attack plan
  -> run normal and adversarial tests
  -> change direction based on results
  -> independently check possible findings
  -> report findings and what remains untested
```

The AI models help decide what to investigate and how to adapt. Merlin itself controls the target,
tools, accounts, permissions, limits, evidence, recovery, and cleanup.

## What happens during a campaign

### 1. The operator describes the job

The operator provides the authorized target, objective, accounts or credentials, limits, and any
important exclusions in plain language.

This description is not permission by itself. Merlin turns it into a specific campaign that the
operator can inspect before target-changing work begins.

### 2. Merlin learns how to use the target

Merlin starts with reconnaissance. Depending on the target, this may establish:

- which page, form, chat box, or endpoint accepts input;
- how the user is signed in;
- how sessions and conversation IDs behave;
- which request means an action was actually submitted;
- what a complete response looks like;
- which target state may change; and
- what needs to be cleaned up afterward.

Merlin may perform a harmless, objective-related interaction to prove that the observed path really
works. Merely opening a page or receiving an HTTP response is not enough.

### 3. The operator reviews the campaign

Before approval, the operator should be able to see:

- the exact target and objective;
- the planned tests;
- the accounts, browser profile, or API access involved;
- the tools and models assigned to the work;
- time, token, request, and cost limits;
- what evidence each test expects;
- which actions might change target state;
- how cleanup will be handled; and
- important gaps or assumptions.

The operator can change, narrow, reject, or approve the campaign.

### 4. Merlin runs the investigation

Workers execute focused tests inside the approved boundary. The Campaign Lead watches the results,
stops unproductive repetition, asks for missing evidence, and redirects effort toward better clues.

Some tests use known techniques. Others begin as new hypotheses based on what reconnaissance or an
earlier attack revealed. A new hypothesis must still describe the exact surface, the suspected
weakness, what observation would confirm or reject it, and why existing tests do not already cover
it.

### 5. Possible findings are checked

A worker does not confirm its own claim merely by writing a convincing explanation. Merlin connects
the claim to the real target interaction and, where possible, asks a separately controlled validator
to reproduce or reject it.

### 6. Merlin reports what happened

The final report should show:

- confirmed and reproduced findings;
- rejected claims;
- negative tests;
- blocked, incomplete, or uncertain work;
- the evidence behind each conclusion; and
- important surfaces that were not tested.

A completed campaign does not mean the target is secure. It means the approved work finished within
the available access and budget.

## The Campaign Lead and the team

The Campaign Lead is responsible for the direction of the assessment. It looks across the work,
connects related observations, decides which clues deserve more attention, and changes the plan as
evidence arrives.

The Lead does not browse or attack the target directly. It also cannot give itself or another agent
new access. Merlin checks every proposed action against the approved campaign.

### How the team works today

The current scheduler assigns work to a small set of predefined security roles. The Lead can
redirect those workers, request follow-up work, or stop them. Extra roles can be configured before
the campaign starts.

This works, but it is rigid. A newly discovered problem may not fit neatly into “authentication,”
“injection,” or “business logic.” Adding more permanent roles would make the roster larger without
necessarily making the scan better.

### What we are building next

We plan to let the Lead build a small team for each target and adjust it as results come in.

For example, if reconnaissance discovers that browser identity, API session, and conversation ID
are handled separately, the Lead could request a short-lived specialist focused on that exact
combination. When the work finishes or stops producing value, Merlin removes that specialist from
the active campaign.

The Lead can suggest:

- what the specialist should investigate;
- which observations led to the idea;
- what knowledge and capabilities it needs;
- what evidence would prove or disprove the idea; and
- when the specialist should stop.

Merlin still decides:

- which target and accounts it may use;
- which tools and actions are allowed;
- which approved model can perform the work;
- its token, request, time, and cost limits;
- its lifetime inside the campaign; and
- who owns cleanup.

The flexible-team system is planned and is not current runtime behavior. Today, a temporary Lead
instruction redirects an existing worker; it does not create a new agent or permission identity.

### Roles that remain separate

A few responsibilities should remain separate because they create useful safety boundaries:

- planning the campaign;
- running actions against the target;
- creating an automated procedure;
- reviewing that procedure;
- executing the approved procedure; and
- checking a possible finding.

These are not permanent vulnerability categories. They prevent one agent from proposing, approving,
executing, and validating the same action by itself.

## How Merlin keeps flexible agents under control

The Lead and workers are not trusted as permission authorities. Their messages are treated as
proposals and data.

Merlin applies several rules around them:

- A target discovered in a page, document, or tool response does not become authorized.
- A worker receives only the access and tools needed for its assignment.
- A new mission cannot expand the campaign's target, accounts, or budget.
- Credentials are passed as local handles rather than copied into prompts, logs, or reports.
- Target content cannot change an agent's tool permissions.
- A proposed automated flow must be reviewed before it can run.
- Target-changing work receives an identity before execution so later evidence can be traced back.
- If an external action may have happened but Merlin cannot tell, the result remains uncertain and
  is not blindly repeated.
- Cleanup work survives worker failure and campaign cancellation where the target allows it.

The planned dynamic-team rollout initially keeps the current maximum of three target-capable workers
running at once and limits a campaign to twelve created specialists, including retired ones. These
limits can change later only after measured results justify it.

## Normal tests and attack tests

Where possible, Merlin compares an attack with a normal version of the same action.

Suppose the question is whether test user B can open test user A's conversation:

1. User A opens their own conversation. This proves the feature works normally.
2. User B attempts the equivalent request using A's conversation ID.
3. Merlin compares the response and any resulting state.
4. A separately controlled test attempts to reproduce the result.
5. User A cleans up any test data if necessary.

The normal action does not make the attack stronger. It makes the conclusion more credible. If the
normal request fails too, or both requests return the same meaningless response, Merlin should not
report a confident authorization flaw.

Not every security question has a useful control. When it does not, Merlin should say so rather than
invent one.

## Browser and API testing

Merlin can use an isolated browser or an operator-approved persistent browser profile tied to exact
origins. Recognizing a hostname is not enough to gain access to that profile.

During reconnaissance, Merlin can record structured browser API traffic and turn an observed JSON
exchange into a reviewed multi-step API flow. That flow can carry values such as a generated session
ID from one request to the next, use an approved credential or signed-in browser context, and run
cleanup after the test.

This foundation is available today. A general differential mutation engine is not.

The next API capability will automatically compare carefully matched changes such as:

- user or account;
- browser session or API token;
- tenant;
- object or conversation ID;
- claimed role;
- HTTP method; and
- content type or parameter location.

We are starting with cross-user object access because it offers a clear test of whether the new
capability produces findings that the current single-input flow cannot reach. If it does not, the
larger dynamic-team work should pause for review rather than continue on assumption.

## Adaptive attacks

Some weaknesses appear only after several turns. Merlin supports bounded adaptive approaches where
the technique and target allow them.

### Crescendo

Crescendo starts with a milder request and adjusts based on the target's response. It can escalate,
change direction, backtrack after a refusal, or stop when the result is clear or the limit is
reached.

### GOAT

GOAT uses a structured strategy loop. It records the current observation, selects the next tactic,
and continues inside the reviewed turn and authority limits.

### Hydra-Select

Hydra-Select creates several possible attacks, ranks them, and tries only a limited number instead
of spending the target budget on every candidate.

These mechanisms are not excuses for endless retries. A failed attempt should change the next
experiment. Repeating the same request is not adaptation.

## Poison Soup, token bombs, and resource abuse

Some attacks aim to waste resources rather than reveal data. Examples include:

- distracting the agent until it loses the original task;
- flooding its context with irrelevant information;
- provoking unnecessarily large outputs;
- causing recursive or repeated work;
- exhausting the context window;
- increasing latency;
- triggering repeated tool calls; and
- creating unnecessary provider cost.

Merlin already carries attack guidance for these families. The planned measurement layer will run a
normal case and an attack case side by side and record:

- input and output tokens;
- response size;
- latency and recovery time;
- tool-call count;
- truncation or cancellation behavior;
- whether downstream work actually stopped; and
- cost when measured usage and reviewed pricing are available.

These tests must stop before crossing the operator's budget or causing target degradation. Noisy
results should remain intermittent or uncertain instead of becoming findings after one lucky run.

The complete scan-time comparison and confidence calculation are planned. Catalogue coverage alone
must not be described as finished resource-abuse testing.

## Automated browser and API flows

It is wasteful to ask a model to reason through every repeated click, request, and wait. Merlin has
an automation pipeline that separates three jobs:

1. An offline author prepares a limited sequence of existing Merlin operations.
2. A different offline reviewer checks the exact sequence and its expected evidence.
3. Merlin runs only the reviewed version through a non-agent executor.

The sequence is tied to the campaign and target, cannot silently change origin, and must include
cleanup when the request requires it.

Lead automation requests commit to the PostgreSQL `automation_requests` outbox in the same
transaction as their decision. Campaign startup recovery and supervision drain due requests using
expiring claims and compare-and-set ownership. Failed dispatches without a persisted run retry up
to three claims with backoff. Terminal outcomes and their Lead journal entries commit together.
Before invoking the runner, recovery looks up the existing deterministic campaign/request run ID.
Every persisted terminal run is reconstructed, including its evidence and failure, without repeating
the flow. An interrupted run or an expired running lease without a run receipt is reported as
ambiguous and is never automatically replayed: the former runner may have committed target effects.

The foundation exists, but its current practice test mainly checks whether the operation arguments
match their schemas. Planned improvements include running the flow against a controlled fake target,
testing failure and cancellation paths, requiring real evidence rather than a generic call record,
and repeating the flow to expose unreliable behavior.

Generated custom Python or Playwright code does not currently have a safe runtime. We will consider
that only if important attacks cannot be expressed using the reviewed browser, API, or bounded
procedure tools. A sandbox should be built because measured finding yield requires it—not because a
schema happens to mention custom code.

## What counts as evidence

Model explanations can help us understand a result, but they are not proof by themselves.

Useful evidence may include:

- the exact request sent to the target;
- the exact response or selected response field;
- a browser screenshot or visible witness;
- an acknowledged external action;
- the target state before and after the test;
- token, latency, cost, or tool-use measurements;
- the normal comparison result;
- a second reproduction attempt; and
- the identity of the target, test, worker, and campaign that produced it.

Every piece of evidence proves only what it records. A screenshot proves that something appeared on
screen; it does not automatically prove which request caused it. A successful HTTP response proves
that the server replied; it does not automatically prove the requested state change happened.

### Screenshot retention and cleanup

Rendered browser evidence and failure diagnostics share bounded per-context and daemon-wide file,
byte, and identity budgets under `~/.merlin/browser-evidence` and
`~/.merlin/browser-diagnostics`. When a limit is reached, Merlin records the saturation in its
daemon log and continues DOM, text, and network reconnaissance without taking more screenshots.

Merlin does not automatically delete these files because durable campaign records may still refer
to them. To reclaim capacity, stop the Merlin daemon, archive or delete only evidence whose
references are no longer needed, and then restart Merlin so it can perform a bounded, metadata-only
rescan. Deleting files while the daemon is running does not refund the sticky quota and can break
existing evidence references.

## How a result is decided

Merlin prefers the simplest reliable check.

### Exact checks

For structured APIs, Merlin can select a reviewed JSON field and compare it with expected values.
This is usually stronger than asking another model to interpret the whole response.

### Structured interpretation

Natural-language responses sometimes require interpretation. An evaluator may check specific
criteria and quote the exact supporting text. Its answer remains a reviewable judgment, not
independent proof.

### Independent checking

A separately controlled validator may repeat the attack or inspect the evidence. Two models agreeing
is not automatically independent reproduction, especially if they see the same conversation and
make the same assumptions.

### Uncertain remains uncertain

If the evidence cannot support either conclusion, Merlin should say that the result is unclear,
incomplete, blocked, or ambiguous. It should not force everything into pass or fail.

## Result words used by Merlin

| Result | Plain meaning |
|---|---|
| Confirmed | The expected security-relevant behavior was observed |
| Refuted | The completed test supports that the suspected behavior did not occur |
| Blocked | Access, target behavior, policy, or infrastructure prevented the test |
| Unclear | The available evidence cannot decide the question |
| Incomplete | Required steps or evidence are missing |
| Ambiguous | An external action may have happened, but its result cannot be trusted |
| Cancelled | The operator or system stopped the work |
| Intermittent | The result appeared in some bounded trials but did not reproduce reliably |

## How learning works

Merlin does not retrain a model after every campaign. It keeps a controlled history of what was
tested, where it was tested, what happened, and how the result was checked.

On a later comparable campaign, that history can help Merlin:

- avoid repeating a still-current exact negative;
- recheck a previous finding;
- recheck work when the target, API flow, success condition, or evidence age changed;
- prioritize a related but untested variation; or
- suggest a new target-specific hypothesis.

Evidence from a different target, tenant, bot, deployment, account setup, or scenario can inspire a
new test. It cannot silently confirm a finding, reject a hypothesis, or count as coverage.

Old evidence is never proof that the target stayed unchanged. Even recent evidence can be stale when
the target changes without exposing a clear version signal.

## Target-derived hypotheses

If the current catalogue does not cover an observed problem, a worker may propose a new test. It
must answer four questions:

```yaml
surface: Where exactly is the suspected boundary?
assertion: What security property may be broken?
oracle: What observable result would confirm or reject it?
novelty_basis: Which target evidence shows that existing tests are not enough?
```

Example:

```yaml
surface: POST /api/conversations/{id}
assertion: A conversation ID is trusted without checking the signed-in user.
oracle: Test user B can read or change a conversation created by test user A.
novelty_basis: Recon observed browser identity and conversation ID as separate request values.
```

Until it is evaluated and deliberately promoted, this remains a target-specific idea rather than a
permanent rule for every future target.

## How we will measure whether the redesign works

The dynamic-team plan is a hypothesis, not an automatic improvement. We will compare it with the
current fixed-role system on the same held-out vulnerable and safe targets.

Important measurements include:

- known vulnerabilities found;
- findings independently reproduced;
- unsupported or duplicate findings;
- new findings beyond the supplied catalogue;
- time, requests, tokens, and cost per validated finding;
- whether evidence caused a useful change in direction;
- what remained untested; and
- whether permissions, cleanup, cancellation, and restart stayed reliable.

The first benchmark uses vulnerable and benign fixtures across authorization, stateful workflows,
prompt/tool attacks, and resource abuse. If the new API comparison cannot find a seeded flaw that
the current path misses, the more expensive dynamic-team phases pause for review.

We should not claim Merlin finds more vulnerabilities than another tool until both are tested on the
same hidden targets with comparable models, tools, access, and budgets.

## Common security questions

### Can target content take over an agent?

Target content can influence a model's reasoning. Merlin reduces the consequences by keeping target
text separate from permission decisions and checking proposed actions in application code. This does
not make prompt injection impossible. A compromised worker may still waste time or make poor choices
inside its allowed boundary.

### Can an agent add another target or tool?

No. It can suggest work, but the target and tools must already be inside the reviewed campaign. A
page, document, tool response, or model message cannot grant new authority.

### Does signing in authorize every action?

No. Authentication only proves which test identity is being used. Authorization still depends on
the reviewed target, origin, campaign, action, and account boundary.

### Can Merlin guarantee an external action happens only once?

No. Merlin records intent before dispatch, acknowledgements afterward, and uncertain outcomes when
it cannot tell what happened. True exactly-once behavior requires support from the target, such as
idempotency keys or a reliable reconciliation API.

### Is another model an independent validator?

Not necessarily. Independence depends on the session, evidence, assumptions, route, and reproduction
method—not only the model name. A fresh separately controlled target attempt is stronger than asking
another model to agree with the original report.

### Does a matched response prove a vulnerability?

Only if the reviewed check represents a real security boundary and the evidence shows the expected
effect. A weak success condition can still produce a misleading result. That is why controls,
target-state checks, and reproduction matter.

### Can old evidence poison future campaigns?

It can if reused carelessly. Merlin binds reusable evidence to the reviewed target and scenario,
checks its age and identity, and treats different scenarios as inspiration only. Poisoning resistance
still needs stronger hostile evaluation.

### Why not use Codex, Claude, Kimi, or another general agent directly?

Those models can reason about security. Merlin supplies the surrounding control system: reviewed
target scope, credentials, tool permissions, budgets, durable work, evidence, recovery, independent
checking, and reporting. The model is part of the assessment team; it is not the authority database.

### Why not add a permanent specialist for every vulnerability class?

Because target behavior does not follow our organization chart. A flexible mission can combine
session handling, object authorization, stored content, and tool use without creating another
permanent persona. We will keep the fixed path as a fallback until the flexible approach proves its
value.

### Does saturation mean the target is secure?

No. It means no more admitted work is available within the reviewed target, tools, evidence, and
budget. Weak checks, excluded surfaces, missed ideas, and unknown vulnerabilities may remain.

### What would prove the Merlin approach is not worth the complexity?

If a simpler harness using the same models, tools, targets, and budgets produces comparable
validated findings, evidence quality, recovery, and operator effort, Merlin's additional campaign
system has not earned its cost.

## What works today and what remains planned

### Available foundations

- reviewed and durable campaigns;
- browser and JSON API target paths;
- operator-approved authenticated browser contexts;
- captured browser traffic compiled into stateful reviewed API flows;
- treatment and control identities for supported techniques;
- bounded Crescendo, GOAT, and Hydra-Select mechanisms;
- target-derived hypotheses and an exploration profile;
- evidence, finding, validation, reporting, and learning records;
- cancellation, restart recovery, and cleanup tracking;
- optional model routing by reviewed role; and
- separated automation author, reviewer, and deterministic executor.

### Planned or not yet proven

- real campaign-local specialist creation and retirement;
- general cross-user, cross-session, cross-tenant, and object-ID comparison;
- automatic method, content-type, parameter-location, and concurrency mutation;
- behavioral practice runs for generated automation;
- complete scan-time resource-abuse measurements and confidence;
- sandboxed generated Python or Playwright execution;
- fresh multi-session reproduction for every applicable finding;
- a complete evidence drill-down experience;
- public finding-yield superiority over competing harnesses;
- hostile multi-tenant deployment; and
- packaged support beyond the currently reviewed owner-local macOS ARM path.

## Claims we should avoid

Do not say:

- Merlin proves an AI system is secure.
- Merlin has no false positives or false negatives.
- Every catalogue technique executes completely.
- Every finding is independently reproduced.
- The flexible specialist team is already shipped.
- Generated custom code runs safely today.
- A completed campaign covered every attack surface.
- Merlin is certified for every platform or deployment model.
- Merlin outperforms other security tools without a comparable public benchmark.

## A short demo story

When showing Merlin, keep the story simple:

1. Enter an authorized staging objective.
2. Show what reconnaissance learned about sign-in, input, responses, and cleanup.
3. Show the campaign review and explain that nothing starts only because an AI proposed it.
4. Show a normal action followed by an attack that changes one important detail.
5. Show the Lead reacting to the result rather than repeating a failed request.
6. Open a finding and trace it to the actual target evidence.
7. Show what remains blocked, unclear, or untested.

Suggested closing line:

> Merlin is built to do more than run a list of prompts. It investigates the real AI application,
> adapts when the evidence changes, and gives us findings we can inspect and reproduce.

## Related documents

- [`SECURITY-MEETING-PITCH.md`](SECURITY-MEETING-PITCH.md): conversational meeting guide
- [`LEARNING-BEHAVIOR.md`](LEARNING-BEHAVIOR.md): detailed cross-campaign evidence reuse
- [`EXPLORATION-LEARNING-PROFILE-PLAN.md`](EXPLORATION-LEARNING-PROFILE-PLAN.md): exploration profile
  implementation and acceptance status
- [`plans/dynamic-specialist-swarm-plan.md`](plans/dynamic-specialist-swarm-plan.md): approved dynamic
  specialist and API-differential implementation plan
- [`plans/offensive-scan-effectiveness.md`](plans/offensive-scan-effectiveness.md): offensive finding-yield
  direction
