CREATE TABLE merlin_storage.campaign_capability_observations (
    id text PRIMARY KEY,
    target_id text NOT NULL,
    adapter_digest text NOT NULL,
    plan_digest text NOT NULL,
    results jsonb NOT NULL,
    results_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX campaign_capability_observations_lookup_idx
    ON merlin_storage.campaign_capability_observations
    (target_id, adapter_digest, plan_digest, created_at DESC);
