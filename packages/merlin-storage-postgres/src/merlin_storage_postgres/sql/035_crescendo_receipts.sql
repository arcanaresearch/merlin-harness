CREATE TABLE merlin_storage.crescendo_step_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    semantic_technique_id text NOT NULL,
    contract_digest text NOT NULL CHECK (contract_digest LIKE 'sha256:%'),
    adapter_digest text NOT NULL CHECK (adapter_digest LIKE 'sha256:%'),
    step_index integer NOT NULL CHECK (step_index >= 0),
    stage text NOT NULL CHECK (stage IN ('probe','escalate','backtrack')),
    scheduler_attempt_id uuid NOT NULL REFERENCES merlin_storage.scheduler_attempts(id) ON DELETE RESTRICT,
    effect_id text NOT NULL REFERENCES merlin_storage.tool_effects(idempotency_key) ON DELETE RESTRICT,
    call_id text NOT NULL,
    target_session_id text NOT NULL,
    arguments_digest text NOT NULL,
    prompt_digest text NOT NULL CHECK (prompt_digest LIKE 'sha256:%'),
    exchange_id text NOT NULL,
    exchange_digest text NOT NULL CHECK (exchange_digest LIKE 'sha256:%'),
    observation_digests jsonb NOT NULL CHECK (
        jsonb_typeof(observation_digests) = 'array'
        AND jsonb_array_length(observation_digests) = 4
    ),
    decision text NOT NULL CHECK (decision IN ('success','refusal','progress','no-progress','invalid-premise')),
    receipt_digest text NOT NULL UNIQUE CHECK (receipt_digest LIKE 'sha256:%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id, step_index),
    UNIQUE (effect_id)
);

CREATE TABLE merlin_storage.crescendo_terminal_receipts (
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    assignment_id text NOT NULL,
    semantic_technique_id text NOT NULL,
    adapter_digest text NOT NULL CHECK (adapter_digest LIKE 'sha256:%'),
    contract_digest text NOT NULL CHECK (contract_digest LIKE 'sha256:%'),
    target_session_id text,
    terminal_reason text NOT NULL CHECK (
        terminal_reason IN ('positive','plateau','invalid-premise','capped','cancelled','blocked','ambiguous')
    ),
    verdict text NOT NULL CHECK (verdict IN ('positive','negative','ambiguous','blocked','capped','cancelled')),
    step_receipt_digests jsonb NOT NULL,
    receipt_digest text NOT NULL UNIQUE CHECK (receipt_digest LIKE 'sha256:%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schedule_id, assignment_id)
);

CREATE INDEX crescendo_terminal_receipts_gate_idx
    ON merlin_storage.crescendo_terminal_receipts (schedule_id, verdict);

CREATE FUNCTION merlin_storage.reject_crescendo_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Crescendo receipts are append-only';
END;
$$;

CREATE TRIGGER crescendo_steps_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.crescendo_step_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_crescendo_receipt_mutation();

CREATE TRIGGER crescendo_terminals_append_only
BEFORE UPDATE OR DELETE ON merlin_storage.crescendo_terminal_receipts
FOR EACH ROW EXECUTE FUNCTION merlin_storage.reject_crescendo_receipt_mutation();
