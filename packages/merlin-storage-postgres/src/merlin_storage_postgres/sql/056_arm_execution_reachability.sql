-- Preserve exact transform/reachability receipts across campaign restarts.
ALTER TABLE merlin_storage.campaign_arms
    ADD COLUMN execution_reachability jsonb
    CHECK (
        execution_reachability IS NULL
        OR jsonb_typeof(execution_reachability) = 'object'
    );
