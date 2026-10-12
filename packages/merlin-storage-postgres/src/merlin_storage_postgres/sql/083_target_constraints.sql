-- Structured target rejections can establish reviewed request constraints at
-- runtime. Keep the effective constraint campaign-wide so a restarted worker
-- rejects the same invalid request before another target delivery.
CREATE TABLE merlin_storage.target_constraints (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    target_id text NOT NULL CHECK (target_id <> ''),
    adapter_digest text NOT NULL CHECK (adapter_digest <> ''),
    step_id text NOT NULL CHECK (step_id <> ''),
    request_path text NOT NULL CHECK (request_path <> ''),
    constraint_kind text NOT NULL CHECK (constraint_kind <> ''),
    unit text NOT NULL CHECK (unit IN ('unicode_characters', 'utf8_bytes')),
    maximum integer NOT NULL CHECK (maximum > 0),
    binding_digest text NOT NULL CHECK (binding_digest <> ''),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        campaign_id,
        target_id,
        adapter_digest,
        step_id,
        request_path,
        constraint_kind,
        unit
    )
);

COMMENT ON TABLE merlin_storage.target_constraints IS
    'Evidence-bound request constraints discovered from reviewed structured target rejections.';
