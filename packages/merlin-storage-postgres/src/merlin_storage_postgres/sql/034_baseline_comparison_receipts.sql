CREATE TABLE merlin_storage.baseline_comparison_receipts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id uuid NOT NULL REFERENCES merlin_storage.scheduler_schedules(id) ON DELETE RESTRICT,
    agenda_id text NOT NULL CHECK (agenda_id <> ''),
    pair_id text NOT NULL CHECK (pair_id <> ''),
    policy text NOT NULL CHECK (policy <> ''),
    policy_digest text NOT NULL CHECK (policy_digest LIKE 'sha256:%'),
    oracle_binding_digest text,
    adapter_digest text,
    stage_receipts jsonb NOT NULL,
    transform_artifacts jsonb NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('positive', 'negative', 'ambiguous', 'blocked')),
    reason text,
    receipt_digest text NOT NULL UNIQUE CHECK (receipt_digest LIKE 'sha256:%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, agenda_id)
);

CREATE INDEX baseline_comparison_receipts_gate_idx
    ON merlin_storage.baseline_comparison_receipts (schedule_id, verdict);
