CREATE TABLE merlin_storage.hydra_select_plan_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    semantic_technique_id text NOT NULL,
    contract_digest text NOT NULL CHECK (contract_digest ~ '^sha256:[0-9a-f]{64}$'),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    candidates jsonb NOT NULL,
    selected_candidate_ids jsonb NOT NULL,
    receipt jsonb NOT NULL,
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id),
    UNIQUE (receipt_digest)
);

CREATE TABLE merlin_storage.hydra_select_step_receipts (
    schedule_id uuid NOT NULL,
    assignment_id text NOT NULL,
    step_index integer NOT NULL CHECK (step_index >= 0),
    scheduler_attempt_id uuid NOT NULL REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    effect_id text NOT NULL REFERENCES merlin_storage.tool_effects(idempotency_key) ON DELETE RESTRICT,
    candidate_id text NOT NULL,
    call_id text NOT NULL,
    target_session_id text NOT NULL,
    arguments_digest text NOT NULL CHECK (arguments_digest ~ '^sha256:[0-9a-f]{64}$'),
    exchange_id text NOT NULL,
    exchange_digest text NOT NULL CHECK (exchange_digest ~ '^sha256:[0-9a-f]{64}$'),
    objective_observation_digest text NOT NULL CHECK (objective_observation_digest ~ '^sha256:[0-9a-f]{64}$'),
    objective_matched boolean NOT NULL,
    receipt jsonb NOT NULL,
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id, step_index),
    UNIQUE (schedule_id, assignment_id, call_id),
    UNIQUE (receipt_digest),
    FOREIGN KEY (schedule_id, assignment_id)
        REFERENCES merlin_storage.hydra_select_plan_receipts(schedule_id, assignment_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (schedule_id, scheduler_attempt_id)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (scheduler_attempt_id, effect_id)
        REFERENCES merlin_storage.tool_effects(attempt_id, idempotency_key) ON DELETE RESTRICT
);

CREATE TABLE merlin_storage.hydra_select_terminal_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    terminal_attempt_id uuid REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    blocking_effect_attempt_id uuid REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    blocking_effect_id text REFERENCES merlin_storage.tool_effects(idempotency_key) ON DELETE RESTRICT,
    semantic_technique_id text NOT NULL,
    contract_digest text NOT NULL CHECK (contract_digest ~ '^sha256:[0-9a-f]{64}$'),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_session_id text,
    execution_complete boolean NOT NULL,
    execution_terminal text NOT NULL CHECK (
        execution_terminal IN ('awaiting-judge','cancelled','capped','blocked','ambiguous','stuck')
    ),
    evidence_verdict text NOT NULL CHECK (
        evidence_verdict IN ('positive','negative','ambiguous','blocked')
    ),
    step_receipt_digests jsonb NOT NULL,
    receipt jsonb NOT NULL,
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id),
    UNIQUE (receipt_digest),
    CHECK (
        execution_terminal <> 'blocked'
        OR target_session_id IS NULL
    ),
    FOREIGN KEY (schedule_id, terminal_attempt_id)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (schedule_id, blocking_effect_attempt_id)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (blocking_effect_attempt_id, blocking_effect_id)
        REFERENCES merlin_storage.tool_effects(attempt_id, idempotency_key) ON DELETE RESTRICT
);

CREATE FUNCTION merlin_storage.reject_hydra_select_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Hydra-Select receipts are append-only';
END;
$$;

CREATE TRIGGER hydra_select_plans_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.hydra_select_plan_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_hydra_select_receipt_mutation();

CREATE TRIGGER hydra_select_steps_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.hydra_select_step_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_hydra_select_receipt_mutation();

CREATE TRIGGER hydra_select_terminals_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.hydra_select_terminal_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_hydra_select_receipt_mutation();
