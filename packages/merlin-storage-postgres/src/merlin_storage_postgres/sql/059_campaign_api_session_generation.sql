ALTER TABLE merlin_storage.campaign_api_sessions
    ADD COLUMN generation bigint NOT NULL DEFAULT 1
    CHECK (generation > 0);
