# Stateful execution by default

## Purpose

Make every Merlin interaction a state transition experiment. A target that has no observable mutable state uses the same contract with an empty state snapshot. Stateless concurrency is therefore an optimization that Merlin may enable after it proves independence; it is not a separate execution model.

Rizz or Rekt is an end-to-end acceptance fixture because it combines authentication, mutable runs, ordered progression, turn and daily quotas, multi-message responses, and authoritative outcome fields. Production code must not contain its endpoint names, character names, tier count, score thresholds, or response field names.

## Current rollout status

The preserved Rizz or Rekt campaign failed after nine waves. It attempted 56 of 12,765 candidates, confirmed no finding, never completed Tier 1, reused mutable game runs across independent candidates, and exhausted a daily target quota. The campaign consumed 1,150 model turns and 30.85 million tokens. Worker results, the durable frontier, and draft findings also disagreed about several outcomes.

Phases 0–9 now pass their public-path and PostgreSQL gates. The complete generic fixture matrix also passes through command, TUI, and web interfaces. The remaining rollout work is the fresh live Rizz or Rekt acceptance run and removal of compatibility branches after stored active campaigns migrate or finish.

## Required execution model

Every target action belongs to one `state_scope`:

```text
state scope identity
  -> authoritative snapshot before
  -> one attributable target action
  -> complete response set
  -> authoritative snapshot after
  -> deterministic transition evaluation
  -> candidate evidence and scheduler update
  -> checkpoint, cleanup, or retained-state disposition
```

A state scope records:

- Logical identity: campaign, experiment, candidate, attempt, and action IDs.
- Physical identity: target-authored session, conversation, run, account, job, cart, or equivalent identifiers.
- Baseline and result snapshots, including evidence provenance and freshness.
- Constraints and resources: rendered request limits, turn budgets, rate limits, quotas, deadlines, and supported modalities.
- Isolation disposition: fresh, restored, shared-serialized, contaminated, or unknown.
- Transition disposition: progress, regression, terminal success, terminal failure, unchanged, ambiguous, or infrastructure failure.
- Cleanup disposition and durable recovery information.

Unknown state never becomes evidence of independence. Unknown interpretation does not suppress an authorized attack; Merlin isolates, serializes, validates, or retries it.

## Glossary

- **ARM:** Merlin's target-agnostic adaptive ranking mechanism. It prioritizes concrete candidates from durable evidence; it does not decide whether a candidate remains eligible.
- **Jev:** Merlin's optional target-agnostic model judge. Its score is advisory scheduling evidence and cannot override authoritative state or retire a candidate.
- **Candidate quality:** the canonical vector of valid authoritative support, partial progress, independent reproduction, novelty, target calls, latency, tokens, and cost. Invalid attempts may add retry evidence but cannot decrement support or advance retirement.
- **Principal:** the authenticated or anonymous target identity that owns mutable state or consumes a shared quota. Durable records use a keyed digest or opaque credential reference, never a reusable secret.
- **Physical state:** the target-side object mutated by an action, even when the target rotates its exposed run or session identifier.
- **Authoritative observation:** a value captured from a reviewed target or host-side source with a typed selector and provenance. It is authoritative for the observed transition, not automatically proof of security impact.

Campaign terminal reasons use one closed taxonomy: `objective_satisfied`, `frontier_exhausted`, `budget_exhausted`, `target_terminal`, `operator_stopped`, or `infrastructure_blocked_after_retry_policy`. A quota reset or recoverable infrastructure window suspends a campaign with a wake time; it is not a terminal completion reason.

## Phase dependencies

The numbered phases describe review boundaries, not independent sequential patches. Phases 3–6 are co-developed: baseline gates consume the deterministic oracle, isolation consumes the discovered resource contract, and resource control consumes physical identity. Phases 7–8 are co-developed: outcome normalization is defined before the scheduler uses it. A phase cannot pass its exit gate until all dependencies named by that gate pass.

## Non-negotiable invariants

1. Every delivered action has exactly one durable attempt and one state scope.
2. Every attempt captures a before snapshot, the complete response set, and an after snapshot, or records why each item is unavailable.
3. Independent candidates never share mutable physical state unless the reviewed contract declares the experiment intentionally sequential.
4. Parallel target delivery requires evidence that the physical state scopes are independent. Prompt generation and scoring may remain parallel.
5. A target-authored state transition outranks model interpretation.
6. Harness, provider, transport, constraint, quota, and contamination failures never reduce candidate quality or retire catalogue coverage.
7. A candidate remains queued until it receives a valid terminal disposition or an explicit campaign budget or operator decision stops it.
8. Baseline objectives and ordered progression requirements are executable gates, not prose suggestions.
9. A terminal resource condition stops new target delivery immediately while preserving queued work for a later resumable window.
10. Candidate, attempt, batch, wave, scheduler, and campaign records reconcile through idempotent PostgreSQL transitions.
11. Unknown or low-confidence isolation always defaults to globally leased serialized delivery. It can never imply freshness, independence, or parallel delivery.
12. State and quota coordination spans concurrent campaigns that share a target authority and principal, not only candidates inside one campaign.
13. Raw credentials, cookies, bearer tokens, wallet signatures, and reusable session secrets never enter durable state snapshots, evidence, logs, prompts, or digests.
14. Target-authored state may control target progression decisions, but security impact requires the reviewed oracle's declared trust source or independent host-side corroboration.

## Phase 0 — Preserve the failure and define measurements

- [x] Import the latest failed campaign as a read-only regression corpus with sensitive values redacted.
- [x] Add metrics for unique candidates attempted, physical state scopes used, contaminated attempts, target calls, state transitions, quota consumption, scheduler batches, model turns, tokens, latency, and cost.
- [x] Detect disagreement between worker claims, authoritative transitions, durable frontier outcomes, and finding status.
- [x] Add a campaign diagnostic that explains why execution stopped and which layer produced the terminal condition.
- [x] Establish a small generic fixture matrix: empty-state API, stateful chat, ordered workflow, shared-state-only target, checkpoint target, quota target, and multi-message target.
- [x] Define fixture budgets for target calls, orchestration turns, tokens, elapsed time, and minimum unique valid attempts so correctness cannot pass with zero useful throughput.

**Exit gate:** the diagnostic exactly matches the regression corpus labels for terminal layer, physical-state aliases, quota exhaustion, outcome disagreement, and unfinished frontier. Every fixture rejects a run that exceeds its declared resource budget or produces fewer than its declared unique valid attempts.

## Phase 1 — Universal state-scope envelope

- [x] Create one durable state scope before any target action, including recon and baseline actions.
- [x] Bind generated request IDs and response events to the attempt and scope.
- [x] Capture all response messages until a reviewed completion rule, stream close, or bounded idle deadline.
- [x] Store explicit missing-evidence reasons instead of omitting snapshot fields.
- [x] Represent an empty snapshot for targets with no discovered mutable state.
- [x] Version the state-scope envelope and every compiled selector contract from the first migration.
- [x] Redact or replace sensitive identity values with keyed digests and opaque credential references before persistence or model composition.
- [x] Dual-read old and new records during migration; do not dual-execute target actions.
- [x] Preserve byte-identical immutable evidence and manifest digests for existing records, with explicit versioned derivations for new projections.
- [x] Provide a rollback that stops new-format claims, lets acknowledged attempts settle once, and leaves old-format campaigns resumable by the prior release.
- [x] On rollback, pause campaigns created in the new format after settling acknowledged target effects. Keep their envelopes readable and immutable, and resume them only after the current or a fixed-forward reader returns; never downgrade or reinterpret them through the prior release.

**Exit gate (depends on Phase 0 metrics):** 100% of delivered fixture actions produce attributable before/action/responses/after records or typed missing-evidence reasons through the same public path. Migration tests prove digest equality, secret absence, dual-read behavior, rollback without duplicate target calls, prior-format resumption, and safe pause/fixed-forward resumption of new-format campaigns.

## Phase 2 — Reconnaissance builds the state contract

- [x] Discover candidate physical identity fields from setup and interaction responses.
- [x] Distinguish stable identity, generated-per-action values, generated-per-state values, and session-bound values.
- [x] Discover authoritative progress, score, remaining-resource, terminal, and outcome fields with direction and value semantics.
- [x] Discover fresh-state, reset, restore, clone, and cleanup affordances independently.
- [x] Discover rendered request constraints, supported modalities, rate limits, daily quotas, and retry/reset evidence.
- [x] Bind every selector to an exact adapter step, evaluation root, JSON pointer, evidence sample, and confidence.
- [x] Discover equivalence evidence for rotating identifiers and separate identifier rotation from proven fresh physical state.
- [x] Bind quota ownership to target authority and principal evidence even when run or conversation identifiers rotate.
- [x] Assign each observation source a reviewed trust class: target-declared, host-side reobservation, or independently corroborated.
- [x] Keep uncertain discoveries as advisory evidence; do not turn advertised capabilities or languages into admission filters.

**Exit gate (depends on Phase 1 envelope/versioning):** each stateful fixture compiles a reviewed, target-agnostic contract that can identify its physical state, observe progress, and describe available isolation and resource controls.

## Phase 3 — Baseline and progression gates

- [x] Compile ordered objective clauses into durable phase gates when the operator requests baseline behavior, progression, or prerequisite setup.
- [x] Give each gate a deterministic success oracle, retry policy, resource budget, and failure report.
- [x] Prevent attack batches from starting until required baseline gates succeed or are explicitly waived by the operator.
- [x] Preserve a successful baseline checkpoint or recipe for isolated attack experiments.
- [x] Treat normal-play or workflow agents as bounded controllers driven by authoritative state, not open-ended chat prompts.
- [x] Report an unreachable prerequisite as a named blocker with its last valid state transition.

**Exit gate (depends on Phase 2 contract and Phase 5 oracle):** an ordered-workflow fixture cannot enter its attack phase until every declared prerequisite has a valid authoritative success transition. A failed or ambiguous prerequisite spends zero attack calls and produces a typed blocker.

## Phase 4 — Isolation and concurrency control

- [x] Prefer fresh state, then checkpoint restore, then clone, then exact serialized attribution.
- [x] Lease each physical state to one independent attempt at a time.
- [x] Compare physical identity and freshness evidence before delivery and again after the action.
- [x] Mark aliasing, unexpected prior state, or concurrent mutation as contamination and retry without quality penalty.
- [x] Derive target-delivery concurrency from proven independent state capacity.
- [x] Keep preparation, candidate-payload mutation, and Jev scoring parallel even when target delivery is serialized.
- [x] Prohibit silent fallback from isolated execution to shared mutable state.
- [x] Coordinate physical-state leases in a global registry keyed by target authority, principal digest, and reviewed physical-state equivalence—not campaign ID.
- [x] Treat identifier rotation without freshness evidence as the same conservatively leased physical state.

**Exit gate (depends on Phase 2 contract and Phase 6 resource ledger):** concurrency and cross-campaign stress tests record zero overlapping leases and zero contaminated valid outcomes. Shared-state and rotating-identifier fixtures serialize with exact transition attribution; only reviewed independent states permit parallel target calls.

## Phase 5 — Deterministic transition oracle

- [x] Normalize typed observations without embedding target-specific field names in runtime code.
- [x] Compare before and after snapshots only when their physical identity relationship is valid.
- [x] Resolve ordered progress, score change, remaining resources, terminal success, terminal failure, and custom predicates deterministically.
- [x] Use the complete response set rather than the first assistant message.
- [x] Preserve raw evidence and the rule that produced each transition disposition.
- [x] Use model judgment only for semantics absent from the authoritative contract.
- [x] Escalate conflicts between model judgment and authoritative evidence instead of silently rewriting either result.
- [x] Validate observation types, bounds, monotonic rules, identity relationships, and cross-source consistency before an observation can steer scheduling.
- [x] Quarantine hostile or inconsistent target-declared fields and require host-side or independent corroboration for claimed security impact.

**Exit gate (depends on Phase 1 envelope and Phase 2 contract):** fixtures produce byte-identical transition dispositions without an LLM. Model output cannot convert a target-authored regression into support or a success into failure, and forged or inconsistent progress fields cannot change scheduling until corroborated.

## Phase 6 — Constraints and resource control

- [x] Validate the fully rendered request immediately before delivery.
- [x] Regenerate a bounded child candidate when satisfying a constraint changes payload semantics; preserve immutable parent/child history.
- [x] Reserve expected target resources before dispatch and settle actual consumption afterward.
- [x] Share quota and rate-limit observations across all scopes that use the same target identity.
- [x] On a terminal quota or retry window, stop new delivery, cancel only work that has not reached the target, and persist a resumable wake time.
- [x] Distinguish target refusal from pre-inference rejection, provider failure, harness failure, and operator cancellation.
- [x] Prevent retries from consuming quota when the same blocking condition is already authoritative.

**Exit gate (depends on Phase 2 contract and Phase 4 global registry):** no oversized request reaches the target. After the first authoritative quota block, target-call count remains unchanged until the persisted reset time; queued work then resumes exactly once across restart and concurrent campaigns.

## Phase 7 — Bounded adaptive scheduling

- [x] Keep the complete candidate frontier normalized and durable; never serialize thousands of candidate IDs into a model prompt or scheduler graph.
- [x] Claim a batch sized from proven target concurrency and remaining resource budget.
- [x] Rank concrete candidates from authoritative outcomes, partial progress, novelty, coverage, confidence, target calls, latency, tokens, and cost.
- [x] Run bounded Jev scoring concurrently and treat missing or low scores as scheduling evidence, not retirement evidence.
- [x] Promote related mutations after measured progress and retry invalid attempts through the correct recovery path.
- [x] Settle a batch exactly once, update ARM evidence, and claim the next batch while eligible work and budget remain.
- [x] Add serialized-size and prompt-size guards before persistence and model composition.
- [x] Enforce per-campaign service objectives declared at preparation: maximum orchestration overhead per target call, maximum cost per unique valid attempt, and minimum valid-attempt throughput within the target resource budget.

**Exit gate (depends on Phase 6 resource control and Phase 8 canonical normalization):** a multi-batch PostgreSQL campaign advances automatically, never creates an oversized JSONB or prompt payload, meets its declared throughput and overhead objectives, and changes second-batch ranking from first-batch transitions without removing unselected coverage.

## Phase 8 — Outcome and finding integrity

- [x] Define one normalization table for worker claims, attempt validity, authoritative transitions, ARM outcomes, frontier state, and finding status.
- [x] Prevent defensive behavior, penalties, and explicit refusal from becoming vulnerability findings without demonstrated security impact.
- [x] Require evidence-bound reproduction for supported findings when ambiguity, severity, or policy requires it.
- [x] Keep infrastructure failure, target blocking, refutation, partial progress, and support distinct through final reporting.
- [x] Show attempted unique candidates separately from repeated attempts and validation runs.
- [x] Reject campaign completion summaries that contradict durable attempt or frontier records.
- [x] Specify the exact normalization relation: projections may add presentation metadata, but candidate ID, validity, outcome, evidence digest, and terminality must match the canonical attempt ledger exactly.

**Exit gate (depends on Phase 1 envelope and Phase 5 oracle):** worker, frontier, ARM, finding, and report projections match canonical candidate ID, validity, outcome, evidence digest, and terminality for every fixture. Any mismatch fails completion, and the preserved campaign cannot report defensive observations as validated vulnerabilities.

## Phase 9 — Durable lifecycle and restart reconciliation

- [x] Persist target-state leases, batch claims, scheduler ownership, quota wake times, and cleanup obligations transactionally.
- [x] Reconcile scheduler, wave, batch, attempts, state scopes, and campaign status on startup.
- [x] Finalize a wave when its scheduler completed, resume only an active scheduler, and recover a missing scheduler from the durable checkpoint.
- [x] Make settlement, cleanup, and next-batch claims idempotent under process death and duplicate delivery.
- [x] Test every persistence boundary against PostgreSQL with forced process interruption.
- [x] Ensure daemon restart never changes campaign meaning or loses an acknowledged target effect.
- [x] Reconcile the global physical-state lease and quota registry before any campaign resumes target delivery.

**Exit gate (depends on Phases 1, 4, 6, 7, and 8):** restart tests at every boundary produce the same final records and target-call count as uninterrupted execution.

## Phase 10 — End-to-end acceptance and rollout

- [x] Run the complete fixture matrix through TUI, web, and command interfaces.
- [x] Verify empty-state targets retain safe concurrency under the universal envelope.
- [x] Verify stateful targets isolate or serialize by evidence rather than target name.
- [x] Verify multi-message capture, request regeneration, quota suspension, adaptive batches, and restart recovery together.
- [ ] Run a fresh Rizz or Rekt campaign with a new quota window and retained PostgreSQL evidence.
- [ ] Require baseline Tier 1 success and Tier 2 entry before attack execution.
- [ ] Demonstrate isolated attack attempts, deterministic score/turn attribution, automatic next-batch selection, and clean stop/resume behavior.
- [ ] Compare target calls, model turns, tokens, elapsed time, and unique valid outcomes with the preserved failed campaign.
- [ ] Make the universal state-scope path the only default after the fixture matrix and live acceptance pass. Remove compatibility branches after stored active campaigns migrate or finish.
- [x] Exercise rollback after migration and after one acknowledged new-format target action before removing the prior reader.

**Exit gate (depends on every Phase 0–9 gate):** all delivered actions have state envelopes; contamination count is zero; post-terminal target calls are zero; projection mismatches are zero; every stop reason belongs to the closed terminal taxonomy; at least two adaptive attack batches complete within their declared call, token, time, and cost budgets. In the live fixture, “normal Tier 1 win” means the target's reviewed success oracle fires before any attack-candidate action is delivered, followed by authoritative Tier 2 entry. Isolated attacks then execute without target-specific runtime code. Winning every tier or finding a vulnerability is not required.

## Test matrix

| Scenario | Required proof |
|---|---|
| Empty-state HTTP API | Empty snapshots, independent scopes, safe bounded concurrency |
| Stateful chat | Complete multi-message response, conversation identity, exact before/after transcript state |
| Ordered workflow | Baseline prerequisite gates attack execution |
| Shared-state-only target | Serialized delivery and exact transition attribution |
| Fresh-state target | Unique physical identity for every independent candidate |
| Checkpoint target | Identical restored baseline with freshness evidence |
| Constraint target | Invalid rendered payload regenerated before delivery |
| Quota target | Global stop, durable wake time, no redundant target calls |
| Concurrent campaigns | Global physical-state lease and quota accounting prevent cross-campaign collision |
| Rotating identifiers | Rotation cannot masquerade as fresh independent physical state |
| Hostile oracle | Forged or inconsistent progress cannot steer scheduling without corroboration |
| Sensitive identity | No reusable credential appears in records, evidence, logs, prompts, or digests |
| Missing oracle | Uncertain result remains eligible and does not block authorized execution |
| PostgreSQL restart | Idempotent reconciliation at every persistence boundary |
| Rizz or Rekt | Normal Tier 1 win, Tier 2 transition, isolated attacks, bounded adaptive continuation |

## Delivery slices

1. **Trustworthy action:** Phases 0–2 establish one attributable, versioned stateful envelope and reviewed contract.
2. **Trustworthy experiment:** Phases 3–6 iterate together to enforce prerequisites, isolation, deterministic outcomes, and resource safety; none passes alone.
3. **Trustworthy campaign:** Phases 7–9 iterate together, defining canonical outcome normalization before adaptive ranking and proving durable recovery before continuous advancement.
4. **Default rollout:** Phase 10 proves the complete design through all interfaces and the live acceptance fixture.

Do not mark a slice complete because unit fixtures pass. Its public-path PostgreSQL gate must pass, and any live acceptance requirement must have retained evidence.
