CREATE UNIQUE INDEX IF NOT EXISTS report_revisions_completion_singleton_idx
    ON merlin_storage.report_revisions (campaign_id)
    WHERE projection->>'completion_digest' IS NOT NULL;
