ALTER TABLE merlin_storage.scheduler_schedules
    DROP CONSTRAINT scheduler_schedules_status_check;
ALTER TABLE merlin_storage.scheduler_schedules
    ADD CONSTRAINT scheduler_schedules_status_check
    CHECK (status IN ('running', 'paused', 'completed', 'failed', 'cancelled'));

ALTER TABLE merlin_storage.assessment_campaigns
    DROP CONSTRAINT assessment_campaigns_status_check;
ALTER TABLE merlin_storage.assessment_campaigns
    ADD CONSTRAINT assessment_campaigns_status_check
    CHECK (status IN ('prepared', 'executing', 'paused', 'completed', 'cancelled', 'failed'));

ALTER TABLE merlin_storage.campaign_waves
    DROP CONSTRAINT campaign_waves_status_check;
ALTER TABLE merlin_storage.campaign_waves
    ADD CONSTRAINT campaign_waves_status_check
    CHECK (status IN ('running', 'paused', 'completed', 'failed', 'cancelled'));

ALTER TABLE merlin_storage.permission_audit
    ADD COLUMN operation_group text;
