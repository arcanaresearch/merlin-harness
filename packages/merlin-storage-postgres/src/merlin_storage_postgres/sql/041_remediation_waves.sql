ALTER TABLE merlin_storage.campaign_waves
    DROP CONSTRAINT IF EXISTS campaign_waves_kind_check;

ALTER TABLE merlin_storage.campaign_waves
    ADD CONSTRAINT campaign_waves_kind_check
    CHECK (kind IN ('initial', 'followup', 'validation', 'remediation'));

COMMENT ON CONSTRAINT campaign_waves_kind_check ON merlin_storage.campaign_waves IS
    'Admits attack, validation, and post-validation defensive remediation wave types.';
