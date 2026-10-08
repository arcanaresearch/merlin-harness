ALTER TABLE merlin_storage.tool_effects
    ADD COLUMN arguments_payload jsonb,
    ADD CONSTRAINT tool_effects_attempt_idempotency_key UNIQUE (attempt_id, idempotency_key);

CREATE TABLE merlin_storage.goat_step_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    step_index integer NOT NULL CHECK (step_index >= 0),
    scheduler_attempt_id uuid NOT NULL REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    effect_id text NOT NULL REFERENCES merlin_storage.tool_effects(idempotency_key) ON DELETE RESTRICT,
    semantic_technique_id text NOT NULL,
    contract_digest text NOT NULL CHECK (contract_digest ~ '^sha256:[0-9a-f]{64}$'),
    adapter_digest text NOT NULL CHECK (adapter_digest ~ '^sha256:[0-9a-f]{64}$'),
    call_id text NOT NULL,
    target_session_id text NOT NULL,
    arguments_digest text NOT NULL,
    response_digest text NOT NULL CHECK (response_digest ~ '^sha256:[0-9a-f]{64}$'),
    telemetry_digest text NOT NULL CHECK (telemetry_digest ~ '^sha256:[0-9a-f]{64}$'),
    strategy_ids jsonb NOT NULL,
    exchange_id text NOT NULL,
    exchange_digest text NOT NULL CHECK (exchange_digest ~ '^sha256:[0-9a-f]{64}$'),
    objective_observation_digest text NOT NULL CHECK (objective_observation_digest ~ '^sha256:[0-9a-f]{64}$'),
    objective_matched boolean NOT NULL,
    strategy_plan jsonb NOT NULL,
    target_reply jsonb NOT NULL,
    target_reply_digest text NOT NULL CHECK (target_reply_digest ~ '^sha256:[0-9a-f]{64}$'),
    tool_content text NOT NULL,
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id, step_index),
    UNIQUE (effect_id),
    UNIQUE (receipt_digest),
    FOREIGN KEY (schedule_id, scheduler_attempt_id)
        REFERENCES merlin_storage.scheduler_attempts(schedule_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (scheduler_attempt_id, effect_id)
        REFERENCES merlin_storage.tool_effects(attempt_id, idempotency_key) ON DELETE RESTRICT
);

CREATE TABLE merlin_storage.goat_terminal_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    semantic_technique_id text NOT NULL,
    contract_digest text NOT NULL CHECK (contract_digest ~ '^sha256:[0-9a-f]{64}$'),
    adapter_digest text NOT NULL CHECK (adapter_digest ~ '^sha256:[0-9a-f]{64}$'),
    target_session_id text,
    target_turns integer NOT NULL,
    execution_terminal text NOT NULL CHECK (
        execution_terminal IN ('max-turns','cancelled','capped','blocked','ambiguous','stuck')
    ),
    execution_complete boolean NOT NULL,
    evidence_verdict text NOT NULL CHECK (
        evidence_verdict IN ('positive','negative','ambiguous','blocked')
    ),
    block_reason text,
    CHECK ((execution_terminal = 'blocked') = (block_reason IS NOT NULL)),
    step_receipt_digests jsonb NOT NULL,
    receipt_digest text NOT NULL CHECK (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id),
    UNIQUE (receipt_digest)
);

CREATE FUNCTION merlin_storage.reject_goat_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'GOAT receipts are append-only';
END;
$$;

CREATE TRIGGER goat_steps_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.goat_step_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_goat_receipt_mutation();

CREATE TRIGGER goat_terminals_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.goat_terminal_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_goat_receipt_mutation();
