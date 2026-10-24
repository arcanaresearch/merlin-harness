ALTER TABLE merlin_storage.target_profiles
    ADD COLUMN browser_session_policy text NOT NULL DEFAULT 'ephemeral'
    CHECK (browser_session_policy IN ('ephemeral', 'persistent'));

COMMENT ON COLUMN merlin_storage.target_profiles.browser_session_policy IS
    'Reviewed target-level browser lifetime policy. Persistent mode may reuse the configured durable browser profile; no origin is hardcoded.';
