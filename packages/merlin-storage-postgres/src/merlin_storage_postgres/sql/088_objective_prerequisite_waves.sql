-- Objective prerequisites run as their own waves after the initial gate.
-- Keep all existing wave kinds valid during upgrades.
ALTER TABLE merlin_storage.campaign_waves
    DROP CONSTRAINT IF EXISTS campaign_waves_kind_check;

ALTER TABLE merlin_storage.campaign_waves
    ADD CONSTRAINT campaign_waves_kind_check
    CHECK (kind IN (
        'initial',
        'followup',
        'frontier',
        'objective_prerequisite',
        'validation',
        'reproduction',
        'review',
        'remediation',
        'operator-continuation'
    ));

COMMENT ON CONSTRAINT campaign_waves_kind_check ON merlin_storage.campaign_waves IS
    'Admits objective prerequisites, attack batches, continuation, validation, reproduction, review, and remediation waves.';
