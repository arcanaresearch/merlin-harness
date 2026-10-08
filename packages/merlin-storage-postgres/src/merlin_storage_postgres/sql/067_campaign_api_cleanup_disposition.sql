ALTER TABLE merlin_storage.campaign_api_sessions
    ADD COLUMN cleanup_disposition text
    CHECK (
        cleanup_disposition IS NULL
        OR cleanup_disposition IN (
            'cleanup_completed',
            'cleanup_not_owned',
            'no_cleanup_obligation'
        )
    );
