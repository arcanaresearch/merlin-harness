CREATE TABLE merlin_storage.baseline_technique_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    agenda_id text NOT NULL CHECK (agenda_id <> ''),
    technique_id text NOT NULL CHECK (technique_id <> ''),
    pair_id text NOT NULL CHECK (pair_id <> ''),
    stage text NOT NULL CHECK (stage IN ('control', 'direct', 'replay')),
    scheduler_attempt_id uuid NOT NULL UNIQUE
        REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    predecessor_id uuid REFERENCES merlin_storage.baseline_technique_attempts(id) ON DELETE RESTRICT,
    prompt_digest text,
    oracle_digest text NOT NULL CHECK (oracle_digest LIKE 'sha256:%'),
    target_session_id text,
    target_session_policy text NOT NULL CHECK (target_session_policy = 'same-reviewed-session'),
    prompt_effect_id text,
    target_effect_id text,
    witness_effect_ids text[] NOT NULL DEFAULT '{}',
    outcome text NOT NULL CHECK (outcome IN ('completed', 'blocked', 'ambiguous')),
    reason text,
    result_digest text NOT NULL CHECK (length(result_digest) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, agenda_id, stage),
    CHECK (outcome <> 'completed' OR
           (prompt_digest IS NOT NULL AND target_session_id IS NOT NULL
            AND prompt_effect_id IS NOT NULL AND target_effect_id IS NOT NULL
            AND cardinality(witness_effect_ids) > 0))
);

CREATE INDEX baseline_technique_attempts_gate_idx
    ON merlin_storage.baseline_technique_attempts (schedule_id, agenda_id, stage, outcome);
