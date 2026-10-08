ALTER TABLE merlin_storage.campaign_lessons
    ADD COLUMN valid_until timestamptz;

UPDATE merlin_storage.campaign_lessons
SET valid_until = created_at;

ALTER TABLE merlin_storage.campaign_lessons
    ALTER COLUMN valid_until SET NOT NULL;

CREATE INDEX campaign_lessons_target_technique_history_idx
    ON merlin_storage.campaign_lessons (target_id, technique_id, created_at DESC);
