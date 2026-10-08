DROP INDEX IF EXISTS merlin_storage.report_revisions_completion_singleton_idx;

CREATE UNIQUE INDEX IF NOT EXISTS report_revisions_completion_digest_idx
    ON merlin_storage.report_revisions (
        campaign_id,
        (projection->>'completion_digest')
    )
    WHERE projection->>'completion_digest' IS NOT NULL;
