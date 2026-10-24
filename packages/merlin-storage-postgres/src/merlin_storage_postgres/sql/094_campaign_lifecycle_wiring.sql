ALTER TABLE merlin_storage.campaign_events DROP CONSTRAINT campaign_events_type_check;
ALTER TABLE merlin_storage.campaign_events ADD CONSTRAINT campaign_events_type_check
    CHECK (type IN ('campaign_paused', 'campaign_resumed', 'campaign_terminal'));

ALTER TABLE merlin_storage.campaign_recovery
    ADD COLUMN fence_digest text CHECK (fence_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD COLUMN application_state text NOT NULL DEFAULT 'planned'
        CHECK (application_state IN ('planned', 'claimed', 'applied', 'rejected')),
    ADD COLUMN claim_id uuid,
    ADD COLUMN claimant_id text,
    ADD COLUMN claim_expires_at timestamptz,
    ADD COLUMN applied_at timestamptz,
    ADD COLUMN application_error text,
    ADD CONSTRAINT campaign_recovery_application_check CHECK (
        (application_state <> 'claimed' OR
            (claim_id IS NOT NULL AND claimant_id IS NOT NULL AND claim_expires_at IS NOT NULL))
        AND (application_state <> 'applied' OR applied_at IS NOT NULL)
        AND (application_state <> 'rejected' OR application_error IS NOT NULL)
    );
