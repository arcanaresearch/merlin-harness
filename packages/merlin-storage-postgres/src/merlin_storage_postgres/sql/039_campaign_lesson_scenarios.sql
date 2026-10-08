ALTER TABLE merlin_storage.campaign_lessons
    ADD COLUMN scenario_fingerprint text;

UPDATE merlin_storage.campaign_lessons
SET scenario_fingerprint = 'legacy:unknown';

ALTER TABLE merlin_storage.campaign_lessons
    ALTER COLUMN scenario_fingerprint SET NOT NULL,
    ADD CONSTRAINT campaign_lessons_scenario_fingerprint_check
        CHECK (
            scenario_fingerprint = 'legacy:unknown'
            OR scenario_fingerprint LIKE 'sha256:%'
        );

DROP INDEX IF EXISTS merlin_storage.campaign_lessons_target_technique_history_idx;
CREATE INDEX campaign_lessons_target_scenario_technique_history_idx
    ON merlin_storage.campaign_lessons
       (target_id, scenario_fingerprint, technique_id, created_at DESC);
