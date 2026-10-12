-- One claimant at a time may reconcile a campaign's scheduler, frontier batch,
-- and wave. Expiry permits recovery after a daemon dies while holding the lease.
CREATE TABLE merlin_storage.campaign_frontier_transition_leases (
    campaign_id text PRIMARY KEY
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    claim_id uuid NOT NULL UNIQUE,
    claimant_id text NOT NULL CHECK (claimant_id <> ''),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX campaign_frontier_transition_lease_expiry_idx
    ON merlin_storage.campaign_frontier_transition_leases (expires_at);

COMMENT ON TABLE merlin_storage.campaign_frontier_transition_leases IS
    'Expiry-fenced ownership for frontier settlement, wave reconciliation, and successor advancement.';
COMMENT ON COLUMN merlin_storage.campaign_frontier_transition_leases.claim_id IS
    'Opaque fencing identity that changes whenever an expired lease is taken over.';

-- Attempt validity is orthogonal to the attack verdict. Legacy target-authored
-- positive/negative rows remain eligible; other historical terminal states are
-- preserved as uncertain rather than inventing quality evidence.
ALTER TABLE merlin_storage.campaign_technique_attempts
    ADD COLUMN disposition text,
    ADD COLUMN disposition_reason text,
    ADD COLUMN physical_state_identity text,
    ADD COLUMN state_identity_match boolean;

UPDATE merlin_storage.campaign_technique_attempts
SET disposition = CASE
        WHEN state IN ('confirmed', 'refuted')
         AND strategy_state -> 'outcome_authority' ->> 'kind' IN ('target', 'validator')
        THEN 'valid'
        ELSE 'uncertain'
    END,
    disposition_reason = CASE
        WHEN state IN ('confirmed', 'refuted')
         AND strategy_state -> 'outcome_authority' ->> 'kind' IN ('target', 'validator')
        THEN 'legacy terminal attempt with explicit authoritative evidence'
        ELSE 'legacy terminal attempt without explicit validity disposition'
    END
WHERE terminal_at IS NOT NULL;

ALTER TABLE merlin_storage.campaign_technique_attempts
    ADD CONSTRAINT campaign_technique_attempts_disposition_check
    CHECK (disposition IN (
        'valid', 'contaminated', 'protocol_invalid', 'infrastructure_failed', 'uncertain'
    )),
    ADD CONSTRAINT campaign_technique_attempts_disposition_pair_check
    CHECK ((disposition IS NULL) = (disposition_reason IS NULL)),
    ADD CONSTRAINT campaign_technique_attempts_disposition_terminal_check
    CHECK (disposition IS NULL OR terminal_at IS NOT NULL),
    ADD CONSTRAINT campaign_technique_attempts_state_identity_check
    CHECK (state_identity_match IS NULL OR physical_state_identity IS NOT NULL);

COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.physical_state_identity IS
    'Exact reviewed physical target-state identity extracted for this attempt.';
COMMENT ON COLUMN merlin_storage.campaign_technique_attempts.state_identity_match IS
    'Whether before/after authoritative observations refer to the same physical state.';

ALTER TABLE merlin_storage.campaign_arm_outcomes
    ADD COLUMN attempt_disposition text,
    ADD COLUMN outcome_authority text;

UPDATE merlin_storage.campaign_arm_outcomes AS outcome
SET attempt_disposition = CASE
        WHEN outcome.outcome = 'infrastructure_failure' THEN 'infrastructure_failed'
        WHEN attempt.disposition = 'valid'
         AND attempt.strategy_state -> 'outcome_authority' ->> 'kind' = 'target'
         AND outcome.outcome IN ('authoritative_success', 'clean_negative')
        THEN 'valid'
        WHEN attempt.disposition = 'valid'
         AND attempt.strategy_state -> 'outcome_authority' ->> 'kind' = 'validator'
         AND outcome.outcome = 'validated_finding'
        THEN 'valid'
        ELSE 'uncertain'
    END,
    outcome_authority = CASE
        WHEN attempt.disposition = 'valid'
         AND attempt.strategy_state -> 'outcome_authority' ->> 'kind' = 'validator'
         AND outcome.outcome = 'validated_finding'
        THEN 'validator'
        WHEN attempt.disposition = 'valid'
         AND attempt.strategy_state -> 'outcome_authority' ->> 'kind' = 'target'
         AND outcome.outcome IN ('authoritative_success', 'clean_negative')
        THEN 'target'
        ELSE 'none'
    END
FROM merlin_storage.campaign_technique_attempts AS attempt
WHERE attempt.id = outcome.technique_attempt_id;

ALTER TABLE merlin_storage.campaign_arm_outcomes
    ALTER COLUMN attempt_disposition SET DEFAULT 'uncertain',
    ALTER COLUMN outcome_authority SET DEFAULT 'none',
    ALTER COLUMN attempt_disposition SET NOT NULL,
    ALTER COLUMN outcome_authority SET NOT NULL,
    ADD CONSTRAINT campaign_arm_outcomes_attempt_disposition_check
    CHECK (attempt_disposition IN (
        'valid', 'contaminated', 'protocol_invalid', 'infrastructure_failed', 'uncertain'
    )),
    ADD CONSTRAINT campaign_arm_outcomes_authority_check
    CHECK (outcome_authority IN ('target', 'validator', 'advisory', 'none')),
    ADD CONSTRAINT campaign_arm_outcomes_learning_authority_check
    CHECK (
        (attempt_disposition = 'valid')
        = (outcome_authority IN ('target', 'validator'))
    );

-- Keep the reviewed physical identity after active ownership is released so a
-- later assignment can detect sequential aliasing, not only concurrent use.
ALTER TABLE merlin_storage.campaign_api_sessions
    ADD COLUMN physical_state_identity text;

CREATE INDEX campaign_api_sessions_physical_state_history_idx
    ON merlin_storage.campaign_api_sessions
    (target_id, physical_state_identity, updated_at DESC)
    WHERE physical_state_identity IS NOT NULL;

-- Reviewed target capacity is claimed before setup, when the physical identity
-- may not exist yet, and remains live through post-state observation. Slot zero
-- preserves serialization for capacity-one targets; larger reviewed capacities
-- admit exactly that many cross-daemon executions. The narrower physical-state
-- claim is established during setup.
CREATE TABLE merlin_storage.target_execution_leases (
    target_id text NOT NULL CHECK (target_id <> ''),
    slot_index integer NOT NULL CHECK (slot_index >= 0),
    claim_id uuid NOT NULL UNIQUE,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    session_scope_id text NOT NULL CHECK (session_scope_id <> ''),
    claimant_id text NOT NULL CHECK (claimant_id <> ''),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id, slot_index)
);

CREATE INDEX target_execution_lease_expiry_idx
    ON merlin_storage.target_execution_leases (expires_at);

COMMENT ON COLUMN merlin_storage.target_execution_leases.slot_index IS
    'One durable slot within the reviewed maximum_parallel_states capacity.';

-- Constraint-driven regeneration appends immutable child candidates. Parents
-- remain in the frontier for audit and may be retried under another contract.
ALTER TABLE merlin_storage.campaign_frontier_candidates
    ADD COLUMN parent_candidate_id text,
    ADD COLUMN lineage_reason text,
    ADD CONSTRAINT campaign_frontier_candidate_parent_fk
        FOREIGN KEY (campaign_id, parent_candidate_id)
        REFERENCES merlin_storage.campaign_frontier_candidates(campaign_id, id)
        ON DELETE RESTRICT,
    ADD CONSTRAINT campaign_frontier_candidate_lineage_check
        CHECK (
            (parent_candidate_id IS NULL) = (lineage_reason IS NULL)
            AND (parent_candidate_id IS NULL OR parent_candidate_id <> id)
        );

CREATE INDEX campaign_frontier_candidate_parent_idx
    ON merlin_storage.campaign_frontier_candidates (campaign_id, parent_candidate_id)
    WHERE parent_candidate_id IS NOT NULL;
