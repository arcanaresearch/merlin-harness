ALTER TABLE merlin_storage.campaign_lessons
    ADD COLUMN technique_id text,
    ADD COLUMN attempt_identity text,
    ADD COLUMN target_fingerprint text,
    ADD COLUMN adapter_fingerprint text,
    ADD COLUMN surface text,
    ADD COLUMN transform_id text,
    ADD COLUMN language text,
    ADD COLUMN oracle_digest text;

UPDATE merlin_storage.campaign_lessons
SET technique_id = 'legacy:' || task_id,
    attempt_identity = 'legacy:' || id,
    target_fingerprint = 'legacy:unknown',
    adapter_fingerprint = 'legacy:unknown',
    surface = 'legacy:unknown',
    transform_id = 'legacy:unknown',
    language = 'und',
    oracle_digest = evidence_digest;

ALTER TABLE merlin_storage.campaign_lessons
    ALTER COLUMN technique_id SET NOT NULL,
    ALTER COLUMN attempt_identity SET NOT NULL,
    ALTER COLUMN target_fingerprint SET NOT NULL,
    ALTER COLUMN adapter_fingerprint SET NOT NULL,
    ALTER COLUMN surface SET NOT NULL,
    ALTER COLUMN transform_id SET NOT NULL,
    ALTER COLUMN language SET NOT NULL,
    ALTER COLUMN oracle_digest SET NOT NULL,
    DROP CONSTRAINT campaign_lessons_campaign_id_wave_task_id_key,
    ADD CONSTRAINT campaign_lessons_task_technique_key
        UNIQUE (campaign_id, wave, task_id, technique_id);

CREATE INDEX campaign_lessons_comparable_attempt_idx
    ON merlin_storage.campaign_lessons
       (target_fingerprint, adapter_fingerprint, attempt_identity, created_at DESC);
