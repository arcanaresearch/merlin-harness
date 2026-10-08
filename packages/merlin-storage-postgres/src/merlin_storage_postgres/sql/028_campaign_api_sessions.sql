CREATE TABLE merlin_storage.campaign_api_sessions (
    campaign_id text PRIMARY KEY
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    target_id text NOT NULL,
    adapter_digest text NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'ambiguous', 'cleaned')),
    variables jsonb NOT NULL DEFAULT '{}'::jsonb,
    pending_step jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX campaign_api_sessions_state_idx
    ON merlin_storage.campaign_api_sessions(state, updated_at);
