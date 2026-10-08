ALTER TABLE merlin_storage.campaign_api_sessions
    ADD COLUMN session_scope_id text NOT NULL DEFAULT 'legacy:campaign',
    ADD COLUMN remote_session_id text;

ALTER TABLE merlin_storage.campaign_api_sessions
    DROP CONSTRAINT campaign_api_sessions_pkey,
    ADD PRIMARY KEY (campaign_id, target_id, session_scope_id);

CREATE UNIQUE INDEX campaign_api_sessions_live_remote_identity_idx
    ON merlin_storage.campaign_api_sessions(target_id, remote_session_id)
    WHERE remote_session_id IS NOT NULL AND state <> 'cleaned';

CREATE INDEX campaign_api_sessions_campaign_scope_idx
    ON merlin_storage.campaign_api_sessions(campaign_id, session_scope_id);
