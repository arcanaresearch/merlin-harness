CREATE TABLE merlin_storage.campaign_dynamic_specialists (
    id text PRIMARY KEY,
    campaign_id text NOT NULL REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    state text NOT NULL CHECK (state IN ('active', 'retired')),
    admitted_wave integer NOT NULL CHECK (admitted_wave >= 0),
    ttl_waves integer NOT NULL CHECK (ttl_waves BETWEEN 1 AND 8),
    retired_wave integer,
    retirement_reason text,
    proposal_digest text NOT NULL,
    admitted_digest text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, proposal_digest),
    CHECK (
        (state = 'active' AND retired_wave IS NULL AND retirement_reason IS NULL)
        OR (state = 'retired' AND retired_wave IS NOT NULL AND retirement_reason IS NOT NULL)
    )
);

CREATE INDEX campaign_dynamic_specialists_campaign_state_idx
    ON merlin_storage.campaign_dynamic_specialists
    (campaign_id, state, created_at, id);
