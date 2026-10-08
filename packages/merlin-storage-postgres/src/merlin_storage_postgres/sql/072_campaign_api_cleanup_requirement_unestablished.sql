ALTER TABLE merlin_storage.campaign_api_sessions
    DROP CONSTRAINT IF EXISTS campaign_api_sessions_cleanup_disposition_check;

ALTER TABLE merlin_storage.campaign_api_sessions
    ADD CONSTRAINT campaign_api_sessions_cleanup_disposition_check
    CHECK (
        cleanup_disposition IS NULL
        OR cleanup_disposition IN (
            'cleanup_completed',
            'cleanup_not_owned',
            'cleanup_requirement_unestablished',
            'no_cleanup_obligation'
        )
    );
