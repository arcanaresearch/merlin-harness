-- Immutable startup decisions bind lifecycle state to the exact scheduler,
-- wave, frontier batch, and target-effect topology observed in one transaction.
CREATE TABLE merlin_storage.campaign_recovery (
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    state_digest text NOT NULL CHECK (state_digest ~ '^sha256:[0-9a-f]{64}$'),
    decision jsonb NOT NULL CHECK (jsonb_typeof(decision) = 'object'),
    decision_digest text NOT NULL CHECK (decision_digest ~ '^sha256:[0-9a-f]{64}$'),
    first_observed_at timestamptz NOT NULL DEFAULT now(),
    last_observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, state_digest)
);

CREATE INDEX campaign_recovery_latest_idx
    ON merlin_storage.campaign_recovery (campaign_id, last_observed_at DESC);

COMMENT ON TABLE merlin_storage.campaign_recovery IS
    'One immutable startup decision per exact durable campaign topology; repeated recovery only refreshes last_observed_at.';
