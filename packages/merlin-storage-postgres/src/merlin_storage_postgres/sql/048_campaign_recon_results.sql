CREATE TABLE merlin_storage.campaign_recon_results (
    id text PRIMARY KEY,
    target_id text NOT NULL,
    scenario_fingerprint text NOT NULL,
    result jsonb NOT NULL,
    result_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX campaign_recon_results_lookup_idx
    ON merlin_storage.campaign_recon_results (target_id, scenario_fingerprint, created_at DESC);
