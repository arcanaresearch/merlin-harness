CREATE TABLE merlin_storage.campaign_worker_messages (
    id text PRIMARY KEY,
    campaign_id text NOT NULL
        REFERENCES merlin_storage.assessment_campaigns(id) ON DELETE CASCADE,
    wave integer NOT NULL CHECK (wave >= 0),
    source_task_id text NOT NULL,
    destination text NOT NULL CHECK (destination IN ('lead', 'peers', 'lead_and_peers')),
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 4096),
    content_digest text NOT NULL CHECK (content_digest LIKE 'sha256:%'),
    lead_acknowledged_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX campaign_worker_messages_pending_lead_idx
    ON merlin_storage.campaign_worker_messages (campaign_id, created_at, id)
    WHERE lead_acknowledged_at IS NULL AND destination IN ('lead', 'lead_and_peers');
