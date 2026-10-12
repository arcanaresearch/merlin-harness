# Stateful target execution

## Goal

Make Merlin produce trustworthy results against stateless and stateful targets without target-specific runtime code. Reconnaissance records reviewed constraints and state capabilities. Execution derives isolation, validation, attribution, and concurrency from that contract. Target-authored state remains authoritative when available; uncertainty never suppresses an authorized attack.

## Design decisions

- Model independent capabilities and affordances instead of assigning a target to one closed mode.
- Keep target-specific JSON pointers and values in reviewed adapter data.
- Distinguish attempt validity from candidate quality. Contaminated, infrastructure-failed, and uncertain attempts do not retire a candidate or lower its ARM quality.
- Preserve immutable parent/child candidate history when request regeneration changes semantics.
- Settle a batch, reconcile its wave, and advance the frontier under one leased, idempotent transition.
- Preserve full frontier coverage. Bounded execution batches select from it without discarding unscored or low-scoring candidates.

## Contract vocabulary

Reviewed adapters may add:

- `request_constraints`: constraints over the fully rendered request, including exact adapter step ID, request-body root, JSON pointer, constraint kind, maximum, unit, confidence, and evidence bindings that repeat the observed maximum.
- `state_capabilities.identity`: paired baseline/result selectors for the physical remote-state identity. Logical lease and conversation tokens are not physical identity evidence.
- `state_capabilities.observations`: typed target-authored state with explicit baseline and result selectors, such as ordered progress, score, remaining budget, terminal state, or custom values.
- `state_capabilities.freshness_rules`: evidence-backed predicates bound to an exact setup step and response/capture root.
- `state_capabilities.affordances`: independently reviewed target operations such as provisioning state, provisioning demonstrably fresh state, restoring a checkpoint, or resetting state.
- `state_capabilities.maximum_parallel_states`: an evidence-backed concurrency ceiling.

Every selector binds an exact adapter `step_id`, evaluation root, and JSON pointer. Role-only bindings are invalid because an adapter may contain multiple setup or interaction steps. `provision_state` and `provision_fresh_state` are distinct: only a reviewed freshness rule may establish the latter. Serialization is scheduler policy represented by a concurrency ceiling, not a target affordance. Cleanup does not prove reset.

Evidence provenance is retained in the reviewed contract but excluded from executable semantic digests. Changing a pointer, limit, direction, terminal value, affordance, or concurrency ceiling changes executable identity. Adapters that omit all new fields keep their existing digest.

## Phase 0 — Reproduce the failures

- [x] Add generic stateless, fresh-state, shared-state, and missing-oracle fixtures.
- [x] Reproduce multiple logical attempts aliasing fewer physical states.
- [x] Reproduce a rendered request rejected by a discovered length constraint.
- [x] Reproduce a completed scheduler with an active batch and running wave.
- [x] Assert that infrastructure failure and state contamination do not penalize attack quality.

**Gate:** tests fail for the same causal reasons as the preserved campaign.

## Phase 1 — Attempt validity

- [x] Persist `valid`, `contaminated`, `infrastructure_failed`, and `uncertain` attempt dispositions.
- [x] Allow only valid target evidence to affect candidate quality or retirement.
- [x] Keep candidates eligible after invalid attempts.
- [x] Preserve raw evidence and the exact reason an attempt was not valid.

**Gate:** contamination and harness failures cannot become false refutations or successes.

## Phase 2 — State identity and alias detection

- [x] Compile physical-state identity and freshness evidence into the reviewed contract.
- [x] Record logical attempt to physical-state assignments.
- [x] Detect concurrent or sequential aliasing of supposedly isolated attempts.
- [x] Provision state when supported and call it fresh only when a reviewed rule proves freshness.
- [x] Restore a checkpoint when supported.
- [x] Otherwise serialize target interactions and attribute exact before/after transitions.
- [x] Preserve parallel prompt preparation and evaluation when target delivery is serialized.

**Gate:** independent attempts cannot silently share mutable remote state.

## Phase 3 — Settlement, recovery, and advancement

- [x] Settle terminal scheduler results exactly once under a campaign lease.
- [x] Reconcile candidate, attempt, batch, wave, and scheduler state in one recovery operation.
- [x] Use stable idempotency keys derived from campaign, batch, and scheduler result digest.
- [x] Reattach only genuinely active schedules.
- [x] Reduce completed schedules before attempting pause or resume.
- [x] Claim the next bounded batch whenever queued candidates remain.
- [x] Add PostgreSQL restart tests at every persistence boundary.

**Gate:** restart is idempotent and a completed batch cannot strand the frontier.

## Phase 4 — Authoritative state observations

- [x] Capture typed baseline and resulting observations.
- [x] Compile authoritative selectors only when the baseline setup step precedes the interaction result step.
- [x] Compare snapshots only when physical identity matches, unless an explicit transition contract says the identity must change.
- [x] Compute progress, regression, terminal success, and terminal failure from reviewed directions and values.
- [x] Treat target-authored evidence as authoritative over model interpretation.
- [x] Record missing or ambiguous observations as uncertain and continue validation or retry.

**Gate:** a worker cannot contradict authoritative target state when settling an outcome.

## Phase 5 — Request constraints

- [x] Discover constraints from DOM attributes, schemas, client validation, documented interfaces, and structured server rejections.
- [x] Validate the fully rendered request immediately before delivery.
- [x] Generate a bounded child candidate when satisfying a constraint changes semantics.
- [x] Revalidate the child before delivery.
- [x] Feed newly discovered runtime constraints back into reviewed contract evidence.
- [x] Treat protocol rejection as infrastructure or constraint failure, never attack-quality evidence.

**Gate:** an invalid rendered request is not sent repeatedly and is not classified as an attack failure.

## Phase 6 — Adaptive frontier execution

- [x] Record authoritative and model-assisted outcomes after every bounded batch.
- [x] Run independent Jev evaluations concurrently with a bounded wait.
- [x] Keep timed-out or unscored candidates eligible with neutral scheduling evidence.
- [x] Promote related mutations after supported progress.
- [x] Retry infrastructure-failed and contaminated candidates without quality penalties.
- [x] Stop only on objective success, budget exhaustion, operator action, or a typed terminal condition.

**Gate:** completed results automatically produce the next bounded batch while eligible candidates remain.

## Phase 7 — End-to-end validation

- [x] Stateless targets retain safe concurrency.
- [x] Fresh-state targets receive distinct physical identities per independent attempt.
- [x] Shared-state targets run serialized with exact transition attribution.
- [x] Checkpoint-capable targets restore their reviewed baseline.
- [x] Missing-oracle targets continue and settle as uncertain where required.
- [x] Runtime-discovered limits regenerate bounded child candidates.
- [x] Multi-message responses remain complete.
- [x] A PostgreSQL campaign advances across multiple batches and survives restart.
- [ ] A fresh Rizz or Rekt campaign executes clean Tier 1–4 attempts with authoritative state evidence.

**Acceptance:** Merlin produces uncontaminated, explainable outcomes and continues adaptive execution. Winning every target is not required.

## Verification status

The generic implementation and fixture matrix are complete. The affected package suite passes serially against PostgreSQL. The remaining unchecked item is the live Rizz or Rekt acceptance campaign; it requires a fresh target run and is not inferred from fixture coverage.

## Delivery order

The smallest trustworthy slice is Phases 1–3: identify invalid attempts, detect physical-state aliasing, and reliably settle and advance batches. Phases 4–6 then improve evidence quality and adaptive selection without weakening the invariant that uncertain interpretation must not block execution.
