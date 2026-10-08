CREATE TABLE IF NOT EXISTS merlin_storage.report_revisions (
    revision_id text PRIMARY KEY,
    campaign_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    digest text NOT NULL,
    projection jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (campaign_id, revision),
    UNIQUE (campaign_id, digest)
);

CREATE INDEX IF NOT EXISTS report_revisions_campaign_idx
    ON merlin_storage.report_revisions (campaign_id, revision DESC);
